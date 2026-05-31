#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tetris Nutrition Coach — model-agnostic core.

Holds the per-call state, the tool closures, the pipeline assembly, and the
transport branching. The actual AI services are injected via a
``build_services(system_instruction) -> (stt, llm, tts)`` callback so the same
core serves both the Nemotron deploy target (``bot.py``) and the GPT dev
fallback (``bot-gpt.py``) with zero prompt drift — the persona lives once in
``coach_prompt.py``.
"""

import os

import aiohttp
from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndTaskFrame, FunctionCallResultProperties, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.runner.types import (
    DailyRunnerArguments,
    RunnerArguments,
    SmallWebRTCRunnerArguments,
    WebSocketRunnerArguments,
)
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from coach_prompt import build_caller_context, build_system_instruction
from member_store import build_recap, extract_transcript, load_member, save_member
from nutrition_backend import (
    KNOWN_MEMBERS,
    calc_targets,
    find_meals,
    get_meal,
)
from nutrition_backend import (
    search_restaurants as search_restaurants_online,
)

# Required intake fields before targets can be computed (biometrics are optional
# — calc_targets falls back to an activity heuristic without them).
_REQUIRED_PROFILE_FIELDS = ["goal", "diet_style", "activity_level"]

# Conversion constants for US-friendly biometric input.
_LB_TO_KG = 0.453592
_IN_TO_CM = 2.54


async def get_call_info(call_sid: str) -> dict:
    """Fetch call information from Twilio REST API using aiohttp.

    Args:
        call_sid: The Twilio call SID

    Returns:
        Dictionary containing call information including from_number, to_number, status, etc.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if not account_sid or not auth_token:
        logger.warning("Missing Twilio credentials, cannot fetch call info")
        return {}

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"

    try:
        # Use HTTP Basic Auth with aiohttp
        auth = aiohttp.BasicAuth(account_sid, auth_token)

        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=auth) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Twilio API error ({response.status}): {error_text}")
                    return {}

                data = await response.json()

                call_info = {
                    "from_number": data.get("from"),
                    "to_number": data.get("to"),
                }

                return call_info

    except Exception as e:
        logger.error(f"Error fetching call info from Twilio: {e}")
        return {}


def _profile_from_record(record: dict) -> dict:
    """Rebuild a working profile (with targets) from a saved/known record."""
    profile = dict(record["profile"])
    profile.setdefault("allergies", [])
    profile.setdefault("restrictions", [])
    profile["name"] = profile.get("name") or record.get("name")
    profile["targets"] = record["profile"].get("targets") or calc_targets(profile)
    return profile


def _new_state(record: dict | None, member_id: str | None) -> dict:
    """Build per-call state. A returning member starts with profile + targets +
    meal log pre-loaded so intake is skipped; a new caller starts blank.

    Args:
        record: A saved member record (member_store) or a KNOWN_MEMBERS entry.
        member_id: The member's phone number, if known.
    """
    if record:
        return {
            "member_id": member_id,
            "profile": _profile_from_record(record),
            "log": record.get("log") or {"meals": []},
        }
    profile = {
        "name": None,
        "goal": None,
        "diet_style": None,
        "allergies": [],
        "restrictions": [],
        "activity_level": None,
        "sex": None,
        "age": None,
        "weight_kg": None,
        "height_cm": None,
        "targets": None,
    }
    return {"member_id": member_id, "profile": profile, "log": {"meals": []}}


def _missing_fields(profile: dict) -> list:
    """Required intake fields still unset."""
    return [f for f in _REQUIRED_PROFILE_FIELDS if not profile.get(f)]


def _remaining_macros(state: dict) -> dict | None:
    """targets − sum(logged macros). None until targets exist. Values may go
    negative — surfacing an over-budget day honestly is the point."""
    targets = state["profile"].get("targets")
    if not targets:
        return None
    remaining = dict(targets)
    for meal in state["log"]["meals"]:
        for k in ("calories", "protein_g", "carbs_g", "fat_g"):
            remaining[k] -= meal["macros"].get(k, 0)
    return remaining


async def run_bot(
    transport: BaseTransport,
    build_services,
    from_number: str | None = None,
    audio_in_sample_rate: int = 16000,
    audio_out_sample_rate: int = 24000,
):
    """Main bot logic.

    Args:
        transport: The transport to use.
        build_services: ``(system_instruction) -> (stt, llm, tts)`` callback that
            constructs the model-specific services with the persona baked in.
        from_number: Caller's phone number (Twilio path only) for known-member lookup.
        audio_in_sample_rate: Input audio sample rate in Hz. Defaults to 16000 (WebRTC).
        audio_out_sample_rate: Output audio sample rate in Hz. Defaults to 24000 (WebRTC).
    """
    logger.info("Starting bot")

    # Per-call coach state. Closed over by the tool functions below so each call
    # gets its own isolated profile + meal log. A returning member is recognized
    # by caller ID — first from the persistent store, then the demo KNOWN_MEMBERS
    # seed — and starts with profile, targets, and meal log pre-loaded.
    stored = load_member(from_number)
    member = stored or KNOWN_MEMBERS.get(from_number or "")
    state = _new_state(member, from_number)
    connect_recap = build_recap(stored)

    # --- Tools the LLM can call ---------------------------------------------

    async def create_account(
        params: FunctionCallParams, name: str, phone: str | None = None
    ) -> None:
        """Create a member account, OR log an existing member back in. Call this
        when a caller gives their name and phone number. If that phone already has
        an account, this loads their saved profile, meal log, and a recap of last
        time — skip intake and greet them back. Otherwise it starts a new account
        and you continue with intake (goal, diet, allergies, activity).

        Args:
            name: The member's name, in their own words.
            phone: The member's phone number (used to recognize them on future
                calls). Required to save or restore history.
        """
        # The model sometimes passes phone as a number; normalize to a string.
        if phone is not None:
            phone = str(phone).strip()
        existing = load_member(phone) if phone else None
        if existing:
            # Returning member — restore their state and hand back a recap.
            state["profile"] = _profile_from_record(existing)
            state["log"] = existing.get("log") or {"meals": []}
            state["member_id"] = phone.strip()
            await params.result_callback(
                {
                    "ok": True,
                    "returning": True,
                    "name": state["profile"].get("name"),
                    "goal": state["profile"].get("goal"),
                    "targets": state["profile"].get("targets"),
                    "recap": build_recap(existing),
                    "note": (
                        "This member already has an account. Skip intake, greet them back "
                        "warmly, and use the recap for continuity (don't read it aloud)."
                    ),
                }
            )
            return
        state["profile"]["name"] = name.strip()
        if phone:
            state["member_id"] = phone.strip()
            save_history()  # persist immediately so the account exists right away
        await params.result_callback(
            {"ok": True, "returning": False, "name": state["profile"]["name"], "has_phone": bool(phone)}
        )

    # Intake setters: one field per turn. Each returns the still-missing required
    # fields so the model knows when it can compute targets.

    async def set_goal(params: FunctionCallParams, goal: str) -> None:
        """Set the member's nutrition goal.

        Args:
            goal: One of "cut" (lose fat), "maintain", or "bulk" (gain muscle).
        """
        g = goal.strip().lower()
        if g not in ("cut", "maintain", "bulk"):
            await params.result_callback(
                {"ok": False, "reason": "Goal must be cut, maintain, or bulk."}
            )
            return
        state["profile"]["goal"] = g
        await params.result_callback({"ok": True, "goal": g, "missing": _missing_fields(state["profile"])})

    async def set_diet_style(params: FunctionCallParams, diet_style: str) -> None:
        """Set the member's diet style.

        Args:
            diet_style: One of "omnivore", "vegetarian", "vegan", "pescatarian",
                "keto", or "paleo".
        """
        state["profile"]["diet_style"] = diet_style.strip().lower()
        await params.result_callback(
            {"ok": True, "diet_style": state["profile"]["diet_style"], "missing": _missing_fields(state["profile"])}
        )

    async def set_restrictions(
        params: FunctionCallParams,
        allergies: list | None = None,
        restrictions: list | None = None,
    ) -> None:
        """Record allergies and dietary restrictions. Safety-critical: allergens
        recorded here are hard-filtered out of every recommendation.

        Args:
            allergies: Allergen names, lowercase (e.g. "peanut", "shellfish",
                "dairy", "gluten", "soy", "egg", "fish", "tree_nut"). Pass an
                empty list if the member has none.
            restrictions: Other dietary restrictions in the member's words
                (e.g. "no pork", "low sodium"). Optional.
        """
        state["profile"]["allergies"] = [a.strip().lower() for a in (allergies or [])]
        state["profile"]["restrictions"] = [r.strip().lower() for r in (restrictions or [])]
        await params.result_callback(
            {
                "ok": True,
                "allergies": state["profile"]["allergies"],
                "restrictions": state["profile"]["restrictions"],
                "missing": _missing_fields(state["profile"]),
            }
        )

    async def set_activity_level(params: FunctionCallParams, activity_level: str) -> None:
        """Set the member's activity level.

        Args:
            activity_level: One of "sedentary", "light", "moderate", "active",
                or "very_active".
        """
        state["profile"]["activity_level"] = activity_level.strip().lower()
        await params.result_callback(
            {"ok": True, "activity_level": state["profile"]["activity_level"], "missing": _missing_fields(state["profile"])}
        )

    async def set_biometrics(
        params: FunctionCallParams,
        sex: str | None = None,
        age: int | None = None,
        weight_lb: float | None = None,
        height_in: float | None = None,
    ) -> None:
        """Optionally record biometrics for a more precise target. All fields are
        optional — without them targets fall back to an activity heuristic.

        Args:
            sex: "male" or "female" (used by the metabolic formula).
            age: Age in years.
            weight_lb: Body weight in pounds.
            height_in: Height in inches.
        """
        p = state["profile"]
        if sex is not None:
            p["sex"] = sex.strip().lower()
        if age is not None:
            p["age"] = age
        if weight_lb is not None:
            p["weight_kg"] = round(weight_lb * _LB_TO_KG, 1)
        if height_in is not None:
            p["height_cm"] = round(height_in * _IN_TO_CM, 1)
        await params.result_callback({"ok": True, "missing": _missing_fields(p)})

    async def compute_targets(params: FunctionCallParams) -> None:
        """Compute and store the member's daily macro targets from their profile.
        Call this once required intake fields are set, then read the targets back
        for confirmation."""
        missing = _missing_fields(state["profile"])
        if missing:
            await params.result_callback(
                {"ok": False, "missing": missing, "reason": f"Still need: {', '.join(missing)}."}
            )
            return
        targets = calc_targets(state["profile"])
        state["profile"]["targets"] = targets
        save_history()
        await params.result_callback({"ok": True, "targets": targets})

    # Daily recommend.

    async def recommend_meals(
        params: FunctionCallParams,
        slot: str | None = None,
        craving: str | None = None,
        source_type: str | None = None,
        max_results: int = 5,
    ) -> None:
        """Recommend meals that fit the member's remaining macros, diet, and
        allergies. Results are ranked best-fit first.

        Args:
            slot: "breakfast" | "lunch" | "dinner" | "snack" to restrict the slot.
            craving: A craving or keyword in the member's words (e.g. "something
                sweet", "mexican", "quick"). Matched against meal names and tags.
            source_type: "restaurant" | "recipe" | "ready-to-eat" if they want a
                particular kind.
            max_results: Max meals to return (kept small for voice; <= 5).
        """
        profile = state["profile"]
        results = find_meals(
            slot=slot,
            craving=craving,
            source_type=source_type,
            diet_style=profile.get("diet_style"),
            allergies=profile.get("allergies"),
            remaining=_remaining_macros(state),
            max_results=min(max_results, 5),
        )
        if not results:
            await params.result_callback(
                {
                    "meals": [],
                    "note": (
                        "Nothing matched those filters within their diet and allergies. "
                        "Tell them that and offer to widen the search or try a different slot."
                    ),
                }
            )
            return
        await params.result_callback({"meals": results, "remaining": _remaining_macros(state)})

    async def search_restaurants(
        params: FunctionCallParams,
        location: str,
        cuisine: str | None = None,
        max_results: int = 4,
    ) -> None:
        """Search the web for real restaurants near the member's location. Use
        this when they want to eat out somewhere nearby. Ask for their city or
        neighborhood first if you don't have it. Results are real places without
        exact macros — after they pick one, help them choose a dish that fits
        their remaining macros and log it.

        Args:
            location: City, neighborhood, or address to search near (required).
            cuisine: Optional cuisine or keyword (e.g. "sushi", "salad",
                "healthy", "mexican").
            max_results: Max restaurants to return (kept small for voice; <= 5).
        """
        results = await search_restaurants_online(
            location=location, query=cuisine, max_results=min(max_results, 5)
        )
        if not results:
            await params.result_callback(
                {
                    "restaurants": [],
                    "note": (
                        "No results, or online search is unavailable right now. Ask the "
                        "member to confirm their city, try a different cuisine, or fall "
                        "back to recommend_meals for a place from our list."
                    ),
                }
            )
            return
        await params.result_callback({"restaurants": results})

    # Adapt.

    async def log_meal(
        params: FunctionCallParams,
        name: str | None = None,
        calories: int | None = None,
        protein_g: int | None = None,
        carbs_g: int | None = None,
        fat_g: int | None = None,
        slot: str | None = None,
    ) -> None:
        """Log a meal the member actually ate and recompute remaining macros.

        Provide EITHER a ``name`` from our catalog (macros looked up) OR explicit
        macros for an off-menu meal the member describes.

        Args:
            name: Catalog meal name, if it's one of ours.
            calories: Calories, for an off-menu meal.
            protein_g: Protein grams, for an off-menu meal.
            carbs_g: Carb grams, for an off-menu meal.
            fat_g: Fat grams, for an off-menu meal.
            slot: Which slot this was ("breakfast" | "lunch" | "dinner" | "snack").
        """
        if name:
            meal = get_meal(name)
            if not meal:
                await params.result_callback(
                    {
                        "ok": False,
                        "reason": f"'{name}' isn't on our list — ask for its rough macros and log those instead.",
                    }
                )
                return
            macros = dict(meal["macros"])
            logged_name = name.strip().lower()
            logged_slot = slot or meal["slot"]
        else:
            if calories is None:
                await params.result_callback(
                    {"ok": False, "reason": "Need either a catalog name or at least calories."}
                )
                return
            macros = {
                "calories": calories,
                "protein_g": protein_g or 0,
                "carbs_g": carbs_g or 0,
                "fat_g": fat_g or 0,
            }
            logged_name = "off-menu meal"
            logged_slot = slot or "snack"

        state["log"]["meals"].append({"name": logged_name, "slot": logged_slot, "macros": macros})
        save_history()
        await params.result_callback(
            {"ok": True, "logged": logged_name, "remaining": _remaining_macros(state)}
        )

    async def reoptimize_day(params: FunctionCallParams, note: str | None = None) -> None:
        """Re-plan the rest of the day to close the gap to the member's targets,
        given everything logged so far. Over-budget-aware — if remaining macros
        are negative, surface that and suggest lighter meals.

        Args:
            note: Optional context (e.g. "ate out for lunch") to flavor the plan.
        """
        remaining = _remaining_macros(state)
        if remaining is None:
            await params.result_callback(
                {"ok": False, "reason": "No targets yet — finish intake and compute_targets first."}
            )
            return
        profile = state["profile"]
        over_budget = remaining["calories"] < 0
        suggestions = find_meals(
            craving=None,
            source_type=None,
            diet_style=profile.get("diet_style"),
            allergies=profile.get("allergies"),
            remaining=remaining,
            max_results=3,
        )
        await params.result_callback(
            {
                "ok": True,
                "remaining": remaining,
                "over_budget": over_budget,
                "suggestions": suggestions,
                "note": note,
            }
        )

    # Cross-phase.

    async def get_summary(params: FunctionCallParams) -> None:
        """Read back the day: targets, what's been logged, and remaining macros."""
        await params.result_callback(
            {
                "targets": state["profile"].get("targets"),
                "logged": state["log"]["meals"],
                "remaining": _remaining_macros(state),
            }
        )

    async def end_call(params: FunctionCallParams) -> None:
        """End the call. Only call this AFTER you have said goodbye to the
        member in the same turn. The pipeline will flush any queued speech
        and then hang up."""
        logger.info("end_call invoked — pushing EndTaskFrame upstream")
        save_history()
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        # run_llm=False prevents the LLM from generating a follow-up response
        # after this function returns — the goodbye should already be in flight.
        await params.result_callback(
            {"ok": True}, properties=FunctionCallResultProperties(run_llm=False)
        )

    tool_functions = [
        create_account,
        set_goal,
        set_diet_style,
        set_restrictions,
        set_activity_level,
        set_biometrics,
        compute_targets,
        recommend_meals,
        search_restaurants,
        log_meal,
        reoptimize_day,
        get_summary,
        end_call,
    ]
    tools = ToolsSchema(standard_tools=tool_functions)

    # --- System instruction (varies based on caller ID) ---------------------

    caller_context = build_caller_context(member)
    system_instruction = build_system_instruction(caller_context)

    # Model-specific services, persona baked in. (stt, llm, tts) — see bot.py /
    # bot-gpt.py for the two implementations.
    stt, llm, tts = build_services(system_instruction)

    # ToolsSchema describes the tools to the LLM; register_direct_function
    # wires the actual handlers the LLM will invoke. Both are required.
    for fn in tool_functions:
        llm.register_direct_function(fn)

    context = LLMContext(tools=tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # Slightly stricter VAD than the defaults (confidence 0.7 / start 0.2 /
            # min_volume 0.6) so brief background noise doesn't register as speech.
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(confidence=0.8, start_secs=0.3, stop_secs=0.4, min_volume=0.6)
            ),
            # Use Pipecat's default VAD + transcription start chain so phone callers
            # can barge in immediately, including with short corrections like "no".
            # The default smart-turn STOP strategy still handles endpointing.
            user_turn_strategies=UserTurnStrategies(),
        ),
    )

    def save_history():
        """Persist this member's profile, meal log, and transcript — but only if
        they're registered (we have a phone number to key on)."""
        if not state.get("member_id"):
            return  # anonymous caller never gave a number; nothing to save under
        transcript = extract_transcript(context.get_messages())
        save_member(
            state["member_id"],
            state["profile"],
            state["log"],
            transcript,
            name=state["profile"].get("name"),
        )

    # Pipeline - assembled from reusable components
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=audio_in_sample_rate,
            audio_out_sample_rate=audio_out_sample_rate,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        # Kick off the conversation. If caller ID matched a known/returning member,
        # greet them back and hand the model a recap; otherwise give the full intro.
        if member:
            recap_note = (
                f" Here's a recap of last time for your context — do not read it aloud "
                f"verbatim: {connect_recap}"
                if connect_recap
                else ""
            )
            kickoff = (
                "A returning member just called (recognized by caller ID), so skip intake. "
                'Greet them warmly: "Welcome back to your health coach! How can I help '
                'today?" and offer to pick up where you left off.' + recap_note
            )
        else:
            kickoff = (
                "A member just called. Greet them warmly with this opening, in one breath: "
                "\"Hi, I'm your virtual health coach. I can help you figure out the calories "
                "in your food, put together healthy recipes, and find good restaurants "
                "nearby. If you'd like, I can set up an account for you — just give me your "
                'name and phone number."'
            )
        context.add_message({"role": "user", "content": kickoff})
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        save_history()
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)

    await runner.add_workers(worker)
    await runner.run()


async def bot_entry(runner_args: RunnerArguments, build_services):
    """Shared bot entry point. Resolves the transport (WebRTC dev / Twilio phone)
    and hands off to ``run_bot`` with the injected service builder.

    Args:
        runner_args: Pipecat runner arguments selecting the transport.
        build_services: ``(system_instruction) -> (stt, llm, tts)`` callback.
    """
    from_number: str | None = None
    transport_overrides: dict = {}

    # Krisp is available when deployed to Pipecat Cloud
    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter

        krisp_filter = KrispVivaFilter()
    else:
        krisp_filter = None

    match runner_args:
        case DailyRunnerArguments():
            # Pipecat Cloud injects DailyRunnerArguments (and this is the path
            # Cekura's scenarios_run_pipecat_v2 exercises). transcription_enabled
            # stays False so Daily's Deepgram doesn't run — we use our Nemotron STT.
            transport = DailyTransport(
                runner_args.room_url,
                runner_args.token,
                "Tetris Nutrition Coach",
                DailyParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                    camera_out_enabled=False,
                    transcription_enabled=False,
                ),
            )
        case SmallWebRTCRunnerArguments():
            webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection

            transport = SmallWebRTCTransport(
                webrtc_connection=webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                ),
            )
        case WebSocketRunnerArguments():
            # Twilio media streams are 8 kHz μ-law in both directions.
            # This overrides the default sample rates: 16 kHz in / 24 kHz out.
            transport_overrides["audio_in_sample_rate"] = 8000
            transport_overrides["audio_out_sample_rate"] = 8000

            # Parse Twilio websocket and fetch call information
            _, call_data = await parse_telephony_websocket(runner_args.websocket)

            # Fetch call information from Twilio REST API so we can personalize
            # the bot for known members (see KNOWN_MEMBERS).
            call_info = await get_call_info(call_data["call_id"])
            if call_info:
                from_number = call_info.get("from_number")
                logger.info(f"Call from: {from_number} to: {call_info.get('to_number')}")

            serializer = TwilioFrameSerializer(
                stream_sid=call_data["stream_id"],
                call_sid=call_data["call_id"],
                account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
                auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            )

            transport = FastAPIWebsocketTransport(
                websocket=runner_args.websocket,
                params=FastAPIWebsocketParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                    add_wav_header=False,
                    serializer=serializer,
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(transport, build_services, from_number=from_number, **transport_overrides)

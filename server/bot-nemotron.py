#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tetris Nutrition — adaptive macro coach voice agent (Nemotron stack).

A member calls in and the coach either (a) runs a short voice intake and computes
their daily calorie + macro targets, or (b) recommends meals that fit what's left
of their day and re-optimizes when they've skipped, swapped, or over-eaten —
Tetris's "a meal plan that adapts when life happens", by voice.

All backend calls (meal catalog, target calc, member lookup) are mocked in
``nutrition_backend.py`` behind a data-access layer, so swapping to the real
Tetris API is a one-file change. The persona lives in ``coach_prompt.py``.

Pipeline: Nemotron Speech Streaming STT → Nemotron-3-Super-120B LLM → Gradium TTS,
with direct function tools registered on the LLM context.

Run the bot using::

    uv run bot-nemotron.py
"""

import os

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
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
    RunnerArguments,
    SmallWebRTCRunnerArguments,
    WebSocketRunnerArguments,
)
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.gradium.tts import GradiumTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from coach_prompt import build_caller_context, build_system_instruction
from nemotron_llm import VLLMOpenAILLMService
from nutrition_backend import KNOWN_MEMBERS, calc_targets, find_meals, get_meal
from nvidia_stt import NVidiaWebSocketSTTService

load_dotenv(override=True)


MACRO_KEYS = ("calories", "protein_g", "carbs_g", "fat_g")


def new_call_state() -> dict:
    """Fresh per-call coach state: the member's profile plus today's meal log."""
    return {
        "profile": {
            "goal": None,  # "cut" | "maintain" | "bulk"
            "diet_style": None,  # omnivore | vegetarian | vegan | keto | paleo | pescatarian
            "allergies": [],
            "restrictions": [],
            "activity_level": None,  # sedentary | light | moderate | active | very_active
            "biometrics": {"sex": None, "age": None, "height_cm": None, "weight_kg": None},
            "targets": None,  # {calories, protein_g, carbs_g, fat_g}
        },
        "log": {"meals": []},  # each: {name, slot, calories, protein_g, carbs_g, fat_g}
        # For returning members: macros already consumed today before this call.
        "consumed_baseline": None,
        "member_id": None,
        # Set once set_restrictions has been called (or a known member's profile is
        # loaded) — a deterministic safety gate so meals aren't recommended before
        # allergies have been captured, rather than relying on prompt adherence.
        "allergies_confirmed": False,
    }


def remaining_macros(state: dict) -> dict | None:
    """Macros left for the day = targets − (already-consumed baseline + logged meals).

    Returns None when targets aren't set yet. Values may be negative (over budget) —
    that's intentional and drives the "adapt when life happens" coaching.
    """
    targets = state["profile"].get("targets")
    if not targets:
        return None
    base = state.get("consumed_baseline") or {k: 0 for k in MACRO_KEYS}
    used = {
        k: base.get(k, 0) + sum(m.get(k, 0) for m in state["log"]["meals"]) for k in MACRO_KEYS
    }
    rem = {k: round(targets[k] - used[k]) for k in MACRO_KEYS}
    rem["meals_logged"] = len(state["log"]["meals"])
    return rem


def profile_missing(state: dict) -> list[str]:
    """Required intake fields not yet captured (gate for compute_targets)."""
    p = state["profile"]
    return [f for f in ("goal", "diet_style", "activity_level") if not p.get(f)]


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


async def run_bot(
    transport: BaseTransport,
    from_number: str | None = None,
    audio_in_sample_rate: int = 16000,
    audio_out_sample_rate: int = 24000,
):
    """Main bot logic.

    Args:
        transport: The transport to use.
        from_number: Caller's phone number (Twilio path only) for known-member lookup.
        audio_in_sample_rate: Input audio sample rate in Hz. Defaults to 16000 (WebRTC).
        audio_out_sample_rate: Output audio sample rate in Hz. Defaults to 24000 (WebRTC).
    """
    logger.info("Starting bot")

    # Per-call coach state. Closed over by the tool functions below so each call
    # gets its own isolated profile + meal log.
    state = new_call_state()

    # Returning member? Pre-load their profile + targets + today's remaining macros
    # so we can skip intake and go straight to coaching.
    member = KNOWN_MEMBERS.get(from_number or "")
    if member:
        state["member_id"] = from_number
        prof = member["profile"]
        for key in ("goal", "diet_style", "allergies", "restrictions", "activity_level", "targets"):
            if key in prof:
                state["profile"][key] = prof[key]
        # A known member's allergies are already on file — safe to recommend.
        state["allergies_confirmed"] = True
        rem = member.get("today", {}).get("remaining")
        if rem and state["profile"].get("targets"):
            t = state["profile"]["targets"]
            state["consumed_baseline"] = {k: t[k] - rem.get(k, t[k]) for k in MACRO_KEYS}

    # --- Tools the LLM can call ---------------------------------------------

    # Intake -----------------------------------------------------------------

    async def set_goal(params: FunctionCallParams, goal: str) -> None:
        """Record the member's primary goal. Call this during intake.

        Args:
            goal: One of "cut" (lose fat), "maintain", or "bulk" (gain muscle).
        """
        state["profile"]["goal"] = goal.strip().lower()
        await params.result_callback(
            {"ok": True, "goal": state["profile"]["goal"], "missing": profile_missing(state)}
        )

    async def set_diet_style(params: FunctionCallParams, diet_style: str) -> None:
        """Record the member's diet style.

        Args:
            diet_style: One of "omnivore", "vegetarian", "vegan", "keto", "paleo",
                or "pescatarian".
        """
        state["profile"]["diet_style"] = diet_style.strip().lower()
        await params.result_callback(
            {
                "ok": True,
                "diet_style": state["profile"]["diet_style"],
                "missing": profile_missing(state),
            }
        )

    async def set_restrictions(
        params: FunctionCallParams,
        allergies: list[str] | None = None,
        restrictions: list[str] | None = None,
    ) -> None:
        """Record allergies and dietary restrictions. SAFETY-CRITICAL — these are
        enforced on every meal suggestion, so capture them accurately.

        Args:
            allergies: Foods the member is allergic to, e.g. ["peanuts", "shellfish",
                "dairy", "eggs", "soy", "gluten"].
            restrictions: Lifestyle or religious restrictions, e.g. ["halal", "no pork",
                "gluten-free"].
        """
        if allergies is not None:
            state["profile"]["allergies"] = [a.strip().lower() for a in allergies]
        if restrictions is not None:
            state["profile"]["restrictions"] = [r.strip().lower() for r in restrictions]
        # Calling this (even to record "none") clears the recommendation safety gate.
        state["allergies_confirmed"] = True
        await params.result_callback(
            {
                "ok": True,
                "allergies": state["profile"]["allergies"],
                "restrictions": state["profile"]["restrictions"],
            }
        )

    async def set_activity_level(params: FunctionCallParams, activity_level: str) -> None:
        """Record how active the member is.

        Args:
            activity_level: One of "sedentary", "light", "moderate", "active", or
                "very_active".
        """
        state["profile"]["activity_level"] = activity_level.strip().lower()
        await params.result_callback(
            {
                "ok": True,
                "activity_level": state["profile"]["activity_level"],
                "missing": profile_missing(state),
            }
        )

    async def set_biometrics(
        params: FunctionCallParams,
        sex: str | None = None,
        age: int | None = None,
        height_cm: float | None = None,
        weight_kg: float | None = None,
    ) -> None:
        """OPTIONAL — record body stats only if the member volunteers them. Improves
        target accuracy (Mifflin–St Jeor). Never push for these.

        Args:
            sex: "male" or "female" (used for the BMR formula).
            age: Age in years.
            height_cm: Height in centimeters.
            weight_kg: Weight in kilograms.
        """
        bio = state["profile"]["biometrics"]
        if sex is not None:
            bio["sex"] = sex.strip().lower()
        if age is not None:
            bio["age"] = age
        if height_cm is not None:
            bio["height_cm"] = height_cm
        if weight_kg is not None:
            bio["weight_kg"] = weight_kg
        await params.result_callback({"ok": True, "biometrics": bio})

    async def compute_targets(params: FunctionCallParams) -> None:
        """Compute daily calorie + macro targets from the collected profile. Call once
        goal, diet style, and activity level are set, then read the targets back to the
        member for confirmation before recommending meals."""
        missing = profile_missing(state)
        if missing:
            await params.result_callback(
                {"ok": False, "missing": missing, "note": f"Still need: {', '.join(missing)}."}
            )
            return
        targets = calc_targets(state["profile"])
        state["profile"]["targets"] = {k: targets[k] for k in MACRO_KEYS}
        await params.result_callback(
            {"ok": True, "targets": state["profile"]["targets"], "basis": targets["basis"]}
        )

    # Daily recommendation ---------------------------------------------------

    async def recommend_meals(
        params: FunctionCallParams,
        slot: str | None = None,
        craving: str | None = None,
        source_type: str | None = None,
        max_results: int = 5,
    ) -> None:
        """Suggest meals that fit the member's REMAINING macros today, honoring their
        diet style, allergies, and restrictions. Call when the member asks what to eat.

        Args:
            slot: "breakfast", "lunch", "dinner", or "snack". Optional.
            craving: What they're in the mood for, e.g. "chicken", "pasta", "sushi".
                Optional.
            source_type: "restaurant", "recipe", or "ready-to-eat" if they specify how
                they want it. Optional.
            max_results: Max meals to return (hard-capped at 5).
        """
        rem = remaining_macros(state)
        if rem is None:
            await params.result_callback(
                {
                    "meals": [],
                    "note": "No targets yet — run the short intake and call compute_targets first.",
                }
            )
            return
        if not state.get("allergies_confirmed"):
            await params.result_callback(
                {
                    "meals": [],
                    "note": "Ask about allergies and restrictions first, then call set_restrictions (even to record none) before recommending.",
                }
            )
            return
        p = state["profile"]
        meals = find_meals(
            remaining={k: rem[k] for k in MACRO_KEYS},
            diet_style=p["diet_style"],
            allergies=p["allergies"],
            restrictions=p["restrictions"],
            craving=craving,
            source_type=source_type,
            slot=slot,
            max_results=min(max_results, 5),
        )
        await params.result_callback({"meals": meals, "remaining": rem})

    # Adaptation / re-optimization ------------------------------------------

    async def log_meal(
        params: FunctionCallParams,
        name: str | None = None,
        calories: int | None = None,
        protein_g: int | None = None,
        carbs_g: int | None = None,
        fat_g: int | None = None,
        slot: str | None = None,
    ) -> None:
        """Record a meal the member ATE (or swapped/added). If the member states any
        macros, those are used (any they don't give default to zero); otherwise, if
        `name` matches the catalog, macros are filled from it. Recomputes remaining
        macros so you can re-optimize the rest of the day.

        Args:
            name: Meal name (matched against the catalog when no macros are given).
            calories: Calories the member states they ate. Optional.
            protein_g: Protein grams. Optional.
            carbs_g: Carb grams. Optional.
            fat_g: Fat grams. Optional.
            slot: Which meal this was ("breakfast", "lunch", "dinner", "snack"). Optional.
        """
        macros = None
        if any(v is not None for v in (calories, protein_g, carbs_g, fat_g)):
            macros = {
                "calories": calories or 0,
                "protein_g": protein_g or 0,
                "carbs_g": carbs_g or 0,
                "fat_g": fat_g or 0,
            }
        elif name:
            m = get_meal(name)
            if m:
                macros = {k: m["macros"][k] for k in MACRO_KEYS}
        if macros is None:
            await params.result_callback(
                {
                    "ok": False,
                    "note": "Need either a known meal name or the calories/protein for what they ate.",
                }
            )
            return
        entry = {"name": name or "logged meal", "slot": slot, **macros}
        state["log"]["meals"].append(entry)
        rem = remaining_macros(state)
        status = "over" if (rem and rem["calories"] < 0) else "on_track"
        await params.result_callback({"ok": True, "logged": entry, "remaining": rem, "status": status})

    async def reoptimize_day(params: FunctionCallParams, note: str | None = None) -> None:
        """Re-plan the rest of the day after a deviation (skipped, over-ate, ate out).
        Reads current remaining macros and returns meal options that bring the day back
        on target.

        Args:
            note: What happened, e.g. "skipped lunch" or "had a burger out". Optional.
        """
        rem = remaining_macros(state)
        if rem is None:
            await params.result_callback(
                {"options": [], "note": "No targets yet — run intake first."}
            )
            return
        if not state.get("allergies_confirmed"):
            await params.result_callback(
                {
                    "options": [],
                    "note": "Confirm allergies first (call set_restrictions), then re-optimize.",
                }
            )
            return
        p = state["profile"]
        options = find_meals(
            remaining={k: rem[k] for k in MACRO_KEYS},
            diet_style=p["diet_style"],
            allergies=p["allergies"],
            restrictions=p["restrictions"],
            max_results=4,
        )
        if rem["calories"] < 0:
            strategy = "over"
        elif rem["calories"] < 500:
            strategy = "light"
        else:
            strategy = "normal"
        await params.result_callback({"remaining": rem, "options": options, "strategy": strategy})

    async def get_summary(params: FunctionCallParams) -> None:
        """Read back the day: targets, meals logged with macros, and remaining macros."""
        await params.result_callback(
            {
                "targets": state["profile"]["targets"],
                "meals": state["log"]["meals"],
                "remaining": remaining_macros(state),
            }
        )

    async def end_call(params: FunctionCallParams) -> None:
        """End the call. Only call this AFTER you have said goodbye to the member in the
        same turn. The pipeline will flush any queued speech and then hang up."""
        logger.info("end_call invoked — pushing EndTaskFrame upstream")
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        # run_llm=False prevents the LLM from generating a follow-up response
        # after this function returns — the goodbye should already be in flight.
        await params.result_callback(
            {"ok": True}, properties=FunctionCallResultProperties(run_llm=False)
        )

    tool_functions = [
        set_goal,
        set_diet_style,
        set_restrictions,
        set_activity_level,
        set_biometrics,
        compute_targets,
        recommend_meals,
        log_meal,
        reoptimize_day,
        get_summary,
        end_call,
    ]
    tools = ToolsSchema(standard_tools=tool_functions)

    # --- System instruction (varies based on caller ID) ---------------------

    caller_context = build_caller_context(member)
    system_instruction = build_system_instruction(caller_context)

    # Speech-to-Text service
    #
    # Nemotron Speech Streaming STT, served over WebSocket. The server expects
    # 16-bit PCM, 16 kHz, mono; the service resamples input audio to that rate, so
    # both the WebRTC (16 kHz) and Twilio telephony (8 kHz) paths work. The URL can
    # be overridden via NVIDIA_ASR_URL.
    stt = NVidiaWebSocketSTTService(
        url=os.getenv("NVIDIA_ASR_URL", "ws://192.168.7.228:8081"),
        strip_interim_prefix=True,
    )

    # LLM service — Nemotron-3-Super-120B served by vLLM (OpenAI-compatible chat
    # completions at /v1). Reasoning ("thinking") defaults OFF for low-latency voice;
    # set NEMOTRON_ENABLE_THINKING=true to enable. VLLMOpenAILLMService reports TTFB to
    # the first non-thinking token. See server/nemotron_llm.py.
    enable_thinking = os.getenv("NEMOTRON_ENABLE_THINKING", "false").lower() == "true"
    llm = VLLMOpenAILLMService(
        api_key=os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY"),  # vLLM ignores unless --api-key set
        base_url=os.getenv("NEMOTRON_LLM_URL", "http://192.168.7.228:8000/v1"),
        settings=VLLMOpenAILLMService.Settings(
            model=os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
            system_instruction=system_instruction,
            extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": enable_thinking}}},
        ),
    )

    # Text-to-Speech service
    tts = GradiumTTSService(
        api_key=os.environ["GRADIUM_API_KEY"],
        settings=GradiumTTSService.Settings(
            voice=os.getenv("GRADIUM_VOICE_ID", "Eu9iL_CYe8N-Gkx_"),
        ),
    )

    # ToolsSchema describes the tools to the LLM; register_direct_function
    # wires the actual handlers the LLM will invoke. Both are required.
    for fn in tool_functions:
        llm.register_direct_function(fn)

    context = LLMContext(tools=tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=FilterIncompleteUserTurnStrategies(),
        ),
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
        # Kick off the conversation
        context.add_message(
            {
                "role": "user",
                "content": "A member just connected. Greet them: 'This is your Tetris nutrition coach. What can I help you fit in today?'",
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)

    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""

    from_number: str | None = None
    transport_overrides: dict = {}

    # Krisp is available when deployed to Pipecat Cloud
    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter

        krisp_filter = KrispVivaFilter()
    else:
        krisp_filter = None

    match runner_args:
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

    await run_bot(transport, from_number=from_number, **transport_overrides)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()

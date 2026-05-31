#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tetris Nutrition Coach — model-agnostic orchestration core (our build).

Holds per-call state (loaded from the single-user ``store``), the tool closures,
the pipeline assembly, and transport branching. The AI services are injected via a
``build_services(system_instruction) -> (stt, llm, tts)`` callback (see app.py) so
the persona + logic stay model-agnostic.

Single-user model: there's one member. On boot we load their durable profile +
today's log from ``store``; if they've onboarded, intake is skipped and the coach
picks up where the day left off. Writes persist immediately so a later same-day
call sees them.
"""

from __future__ import annotations

import os

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

import store
from catalog import find_meals, get_meal, search_restaurants
from nutrition import calc_targets, pick_closest, remaining_macros
from prompt import build_system_instruction

# Required intake fields before targets can be computed (biometrics optional).
_REQUIRED_PROFILE_FIELDS = ("goal", "diet_style", "activity_level")
_LB_TO_KG = 0.453592
_IN_TO_CM = 2.54


def _missing_fields(profile: dict) -> list:
    return [f for f in _REQUIRED_PROFILE_FIELDS if not profile.get(f)]


async def run_bot(
    transport: BaseTransport,
    build_services,
    audio_in_sample_rate: int = 16000,
    audio_out_sample_rate: int = 24000,
):
    """Main bot logic. Single-user state is loaded from ``store`` on entry."""
    logger.info("Starting Tetris Nutrition Coach")

    boot = store.load_state()
    # Per-call state, closed over by the tools. profile + today's log are the
    # single source of truth during the call; we persist on every write.
    state = {
        "profile": boot["profile"],
        "log": boot["log"],
        "is_returning": boot["is_returning"],
    }

    def remaining() -> dict | None:
        return remaining_macros(state["profile"].get("targets"), state["log"]["meals"])

    def persist_profile():
        store.save_profile(state["profile"])

    # --- Tools --------------------------------------------------------------

    async def create_account(params: FunctionCallParams, name: str, phone: str | None = None) -> None:
        """Start an account for a new member. Call this when a new caller gives their
        name (and optionally phone). For a returning member you already have their
        account — don't call this.

        Args:
            name: The member's name, in their own words.
            phone: Optional phone number.
        """
        state["profile"]["name"] = name.strip()
        if phone is not None:
            state["profile"]["phone"] = str(phone).strip()
        persist_profile()
        await params.result_callback({"ok": True, "name": state["profile"]["name"]})

    async def set_goal(params: FunctionCallParams, goal: str) -> None:
        """Set the member's nutrition goal.

        Args:
            goal: One of "cut" (lose fat), "maintain", or "bulk" (gain muscle).
        """
        g = goal.strip().lower()
        if g not in ("cut", "maintain", "bulk"):
            await params.result_callback({"ok": False, "reason": "Goal must be cut, maintain, or bulk."})
            return
        state["profile"]["goal"] = g
        persist_profile()
        await params.result_callback({"ok": True, "goal": g, "missing": _missing_fields(state["profile"])})

    async def set_diet_style(params: FunctionCallParams, diet_style: str) -> None:
        """Set the member's diet style.

        Args:
            diet_style: "omnivore", "vegetarian", "vegan", "pescatarian", or "paleo".
        """
        state["profile"]["diet_style"] = diet_style.strip().lower()
        persist_profile()
        await params.result_callback(
            {"ok": True, "diet_style": state["profile"]["diet_style"], "missing": _missing_fields(state["profile"])}
        )

    async def set_restrictions(
        params: FunctionCallParams, allergies: list | None = None, restrictions: list | None = None
    ) -> None:
        """Record allergies and dietary restrictions. Safety-critical: allergens
        recorded here are hard-filtered out of every recommendation.

        Args:
            allergies: Allergen names, lowercase (e.g. "peanut", "shellfish", "fish",
                "dairy", "gluten", "soy", "egg", "tree_nut"). Empty list if none.
            restrictions: Other restrictions in the member's words (e.g. "no pork").
        """
        state["profile"]["allergies"] = [a.strip().lower() for a in (allergies or [])]
        state["profile"]["restrictions"] = [r.strip().lower() for r in (restrictions or [])]
        persist_profile()
        await params.result_callback({
            "ok": True,
            "allergies": state["profile"]["allergies"],
            "restrictions": state["profile"]["restrictions"],
            "missing": _missing_fields(state["profile"]),
        })

    async def set_activity_level(params: FunctionCallParams, activity_level: str) -> None:
        """Set the member's activity level.

        Args:
            activity_level: "sedentary", "light", "moderate", "active", or "very_active".
        """
        state["profile"]["activity_level"] = activity_level.strip().lower()
        persist_profile()
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
        """Optionally record biometrics for a more precise target. All optional —
        without them targets fall back to an activity heuristic.

        Args:
            sex: "male" or "female".
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
        persist_profile()
        await params.result_callback({"ok": True, "missing": _missing_fields(p)})

    async def compute_targets(params: FunctionCallParams) -> None:
        """Compute and store the member's daily macro targets from their profile.
        Call once required intake fields are set, then read the targets back."""
        missing = _missing_fields(state["profile"])
        if missing:
            await params.result_callback({"ok": False, "missing": missing, "reason": f"Still need: {', '.join(missing)}."})
            return
        targets = calc_targets(state["profile"])
        state["profile"]["targets"] = targets
        persist_profile()
        await params.result_callback({"ok": True, "targets": targets})

    async def recommend_meals(
        params: FunctionCallParams,
        slot: str | None = None,
        craving: str | None = None,
        source_type: str | None = None,
        max_results: int = 5,
    ) -> None:
        """Recommend meals that fit the member's remaining macros, diet, and allergies.

        Args:
            slot: "breakfast" | "lunch" | "dinner" | "snack" to restrict the slot.
            craving: Craving/keyword in the member's words ("mexican", "something sweet").
            source_type: "restaurant" | "recipe" if they want a particular kind.
            max_results: Max meals to return (<= 5).
        """
        profile = state["profile"]
        results = find_meals(
            slot=slot, craving=craving, source_type=source_type,
            diet_style=profile.get("diet_style"), allergies=profile.get("allergies"),
            remaining=remaining(), max_results=min(max_results, 5),
        )
        if not results:
            await params.result_callback({"meals": [], "note": (
                "Nothing matched within their diet and allergies. Tell them and offer to "
                "widen the search or try a different slot.")})
            return
        await params.result_callback({"meals": results, "remaining": remaining()})

    async def search_restaurants_tool(
        params: FunctionCallParams, location: str, cuisine: str | None = None, max_results: int = 4
    ) -> None:
        """Find real nearby restaurant dishes to eat out. Ask for the member's
        neighborhood first if you don't have it. After they pick one, help them fit
        it and log it.

        Args:
            location: City/neighborhood (required).
            cuisine: Optional cuisine/keyword ("sushi", "mexican", "healthy").
            max_results: Max dishes to return (<= 5).
        """
        profile = state["profile"]
        results = search_restaurants(
            location=location, cuisine=cuisine, diet_style=profile.get("diet_style"),
            allergies=profile.get("allergies"), remaining=remaining(), max_results=min(max_results, 5),
        )
        if not results:
            await params.result_callback({"restaurants": [], "note": (
                "No nearby matches within their diet/allergies. Ask them to try a different "
                "cuisine, or fall back to recommend_meals.")})
            return
        await params.result_callback({"restaurants": results, "remaining": remaining()})

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
        macros for an off-menu meal.

        Args:
            name: Catalog meal name, if it's one of ours.
            calories: Calories, for an off-menu meal.
            protein_g: Protein grams, off-menu.
            carbs_g: Carb grams, off-menu.
            fat_g: Fat grams, off-menu.
            slot: "breakfast" | "lunch" | "dinner" | "snack".
        """
        meal = get_meal(name) if name else None
        if name and meal:
            macros = dict(meal["macros"])
            logged_name = meal["name"]
            logged_slot = slot or (meal["slots"][0] if meal.get("slots") else "snack")
        elif calories is not None:
            macros = {"calories": calories, "protein_g": protein_g or 0, "carbs_g": carbs_g or 0, "fat_g": fat_g or 0}
            logged_name = name.strip() if name else "off-menu meal"
            logged_slot = slot or "snack"
        else:
            await params.result_callback({"ok": False, "reason": (
                f"'{name}' isn't on our list — ask for its rough macros and log those." if name
                else "Need either a catalog name or at least calories.")})
            return
        state["log"]["meals"].append({"name": logged_name, "slot": logged_slot, "macros": macros})
        store.save_today_log(state["log"])
        await params.result_callback({"ok": True, "logged": logged_name, "remaining": remaining()})

    async def reoptimize_day(params: FunctionCallParams, note: str | None = None) -> None:
        """Re-plan the rest of the day to close the gap to the member's targets.
        Over-budget-aware: if remaining macros are negative, surface that and suggest
        lighter meals.

        Args:
            note: Optional context ("ate out for lunch") to flavor the plan.
        """
        rem = remaining()
        if rem is None:
            await params.result_callback({"ok": False, "reason": "No targets yet — finish intake and compute_targets first."})
            return
        profile = state["profile"]
        suggestions = find_meals(
            diet_style=profile.get("diet_style"), allergies=profile.get("allergies"),
            remaining=rem, max_results=3,
        )
        await params.result_callback({
            "ok": True, "remaining": rem, "over_budget": rem["calories"] < 0,
            "suggestions": suggestions, "note": note,
        })

    async def optimize_choice(params: FunctionCallParams, options: list) -> None:
        """Given the options the member is choosing between, pick the one that gets
        them CLOSEST to their remaining macros and suggest a tweak to close the gap.
        Use this when the member presents choices ("a burrito bowl, a pizza, or
        pasta — which is best?") or wants something that's a stretch.

        Args:
            options: A list of the options. Each item: {"name": <dish>, and EITHER
                rely on our catalog for macros OR include estimated "calories",
                "protein_g", "carbs_g", "fat_g" if it's an off-menu dish}.
        """
        built = []
        for opt in options or []:
            if not isinstance(opt, dict):
                opt = {"name": str(opt)}
            name = (opt.get("name") or "option").strip()
            cat = get_meal(name)
            if cat:
                macros = dict(cat["macros"])
            else:
                macros = {k: opt.get(k, 0) or 0 for k in ("calories", "protein_g", "carbs_g", "fat_g")}
            built.append({"name": name, "macros": macros})
        if not built:
            await params.result_callback({"ok": False, "reason": "Tell me the options you're choosing between."})
            return
        ranked = pick_closest(built, remaining())
        await params.result_callback({"ok": True, "ranked": ranked, "remaining": remaining(), "best": ranked[0]["name"]})

    async def get_summary(params: FunctionCallParams) -> None:
        """Read back the day: targets, what's logged, and remaining macros."""
        await params.result_callback({
            "targets": state["profile"].get("targets"),
            "logged": state["log"]["meals"],
            "remaining": remaining(),
        })

    async def end_call(params: FunctionCallParams) -> None:
        """End the call. Only call this AFTER saying goodbye in the same turn."""
        logger.info("end_call invoked — pushing EndTaskFrame upstream")
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        await params.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))

    tool_functions = [
        create_account, set_goal, set_diet_style, set_restrictions, set_activity_level,
        set_biometrics, compute_targets, recommend_meals, search_restaurants_tool,
        log_meal, reoptimize_day, optimize_choice, get_summary, end_call,
    ]
    # search_restaurants_tool is exposed to the LLM under the name "search_restaurants".
    search_restaurants_tool.__name__ = "search_restaurants"
    tools = ToolsSchema(standard_tools=tool_functions)

    # --- System instruction + services -------------------------------------
    system_instruction = build_system_instruction(state, remaining())
    stt, llm, tts = build_services(system_instruction)
    for fn in tool_functions:
        llm.register_direct_function(fn)

    context = LLMContext(tools=tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(confidence=0.8, start_secs=0.3, stop_secs=0.4, min_volume=0.6)
            ),
            # Start on VAD so callers can barge in immediately, even with one word.
            # The default transcription fallback and smart-turn STOP remain enabled.
            user_turn_strategies=UserTurnStrategies(),
        ),
    )

    pipeline = Pipeline([
        transport.input(), stt, user_aggregator, llm, tts, transport.output(), assistant_aggregator,
    ])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True, enable_usage_metrics=True,
            audio_in_sample_rate=audio_in_sample_rate, audio_out_sample_rate=audio_out_sample_rate,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        if state["is_returning"]:
            kickoff = (
                "Your returning member just called. Skip intake. Greet them warmly: "
                '"Welcome back to Tetris — how\'s the day going?" and offer to help with '
                "their next meal or logging what they ate."
            )
        else:
            kickoff = (
                "A member just called. Greet them warmly, in one breath: \"Hi, I'm your "
                "Tetris nutrition coach. I can help you figure out what to eat to hit your "
                "goals, log what you've had, and find good options nearby. If you'd like, I "
                'can set up your profile — just give me your name."'
            )
        context.add_message({"role": "user", "content": kickoff})
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot_entry(runner_args: RunnerArguments, build_services):
    """Shared entry: resolve transport (Pipecat Cloud Daily / WebRTC dev / Twilio
    phone) and hand off to ``run_bot`` with the injected service builder."""
    transport_overrides: dict = {}

    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter
        krisp_filter = KrispVivaFilter()
    else:
        krisp_filter = None

    match runner_args:
        case DailyRunnerArguments():
            # Pipecat Cloud injects this; also the path Cekura's pipecat_v2 exercises.
            transport = DailyTransport(
                runner_args.room_url, runner_args.token, "Tetris Nutrition Coach",
                DailyParams(
                    audio_in_enabled=True, audio_in_filter=krisp_filter, audio_out_enabled=True,
                    camera_out_enabled=False, transcription_enabled=False,
                ),
            )
        case SmallWebRTCRunnerArguments():
            transport = SmallWebRTCTransport(
                webrtc_connection=runner_args.webrtc_connection,
                params=TransportParams(audio_in_enabled=True, audio_in_filter=krisp_filter, audio_out_enabled=True),
            )
        case WebSocketRunnerArguments():
            transport_overrides["audio_in_sample_rate"] = 8000
            transport_overrides["audio_out_sample_rate"] = 8000
            _, call_data = await parse_telephony_websocket(runner_args.websocket)
            serializer = TwilioFrameSerializer(
                stream_sid=call_data["stream_id"], call_sid=call_data["call_id"],
                account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""), auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            )
            transport = FastAPIWebsocketTransport(
                websocket=runner_args.websocket,
                params=FastAPIWebsocketParams(
                    audio_in_enabled=True, audio_in_filter=krisp_filter, audio_out_enabled=True,
                    add_wav_header=False, serializer=serializer,
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(transport, build_services, **transport_overrides)

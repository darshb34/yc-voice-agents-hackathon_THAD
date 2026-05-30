#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Pipecat Cloud deploy entrypoint — Field & Flower (Nemotron build).

Pipecat Cloud's base image runs ``bot(runner_args)`` from this module. We reuse the
unmodified ``run_bot`` pipeline from ``bot-nemotron.py`` (single source of truth for
the tools, prompt, and pipeline) and only select the transport based on the runner
arguments the platform / local dev provides:

  - ``DailyRunnerArguments``       → DailyTransport. This is what Pipecat Cloud injects,
                                     and the path Cekura's ``scenarios_run_pipecat_v2``
                                     exercises.
  - ``SmallWebRTCRunnerArguments`` → SmallWebRTCTransport (local browser dev).
  - ``WebSocketRunnerArguments``   → Twilio telephony.

VAD note: VAD lives in the user aggregator inside ``run_bot``
(``LLMUserAggregatorParams.vad_analyzer`` → a VADController that broadcasts the
user-started/stopped-speaking frames the Nemotron STT relies on). Transports here
therefore do NOT set their own ``vad_analyzer`` — doing so would double-VAD the
pipeline. This mirrors the original SmallWebRTC config in ``bot-nemotron.py``.

Run locally with the Pipecat dev runner::

    uv run bot.py --transport daily     # or webrtc / twilio
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

from loguru import logger
from pipecat.runner.types import (
    DailyRunnerArguments,
    RunnerArguments,
    SmallWebRTCRunnerArguments,
    WebSocketRunnerArguments,
)
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _load_nemotron() -> ModuleType:
    """Import ``bot-nemotron.py`` (hyphenated filename → load by path).

    Loading under a non-``__main__`` name means that file's own ``if __name__ ==
    "__main__"`` block does not execute; we only get the reusable ``run_bot`` and
    ``get_call_info`` definitions.
    """
    path = SCRIPT_DIR / "bot-nemotron.py"
    spec = importlib.util.spec_from_file_location("bot_nemotron", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load bot module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_nemo = _load_nemotron()
run_bot = _nemo.run_bot
get_call_info = _nemo.get_call_info


def _krisp_filter():
    """Krisp Viva noise filter — available on Pipecat Cloud (``ENV`` != 'local').

    Skipped locally (the dependency isn't installed for dev) and degrades gracefully
    if the import fails so a missing filter never takes the bot down.
    """
    if os.environ.get("ENV") == "local":
        return None
    try:
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter

        return KrispVivaFilter()
    except Exception as e:  # noqa: BLE001 — never fail the call over the noise filter
        logger.warning(f"Krisp filter unavailable, continuing without it: {e}")
        return None


async def bot(runner_args: RunnerArguments) -> None:
    """Pipecat Cloud / runner entry point. Builds the transport, then defers to
    the shared ``run_bot`` pipeline."""
    krisp = _krisp_filter()
    from_number: str | None = None
    overrides: dict = {}

    if isinstance(runner_args, DailyRunnerArguments):
        transport = DailyTransport(
            runner_args.room_url,
            runner_args.token,
            "Field & Flower Bot",
            DailyParams(
                audio_in_enabled=True,
                audio_in_filter=krisp,
                audio_out_enabled=True,
                camera_out_enabled=False,
                transcription_enabled=False,  # use our Nemotron STT, not Daily's
            ),
        )
    elif isinstance(runner_args, SmallWebRTCRunnerArguments):
        transport = SmallWebRTCTransport(
            webrtc_connection=runner_args.webrtc_connection,
            params=TransportParams(
                audio_in_enabled=True,
                audio_in_filter=krisp,
                audio_out_enabled=True,
            ),
        )
    elif isinstance(runner_args, WebSocketRunnerArguments):
        # Twilio media streams are 8 kHz μ-law in both directions.
        overrides["audio_in_sample_rate"] = 8000
        overrides["audio_out_sample_rate"] = 8000

        _, call_data = await parse_telephony_websocket(runner_args.websocket)
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
                audio_in_filter=krisp,
                audio_out_enabled=True,
                add_wav_header=False,
                serializer=serializer,
            ),
        )
    else:
        logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
        return

    await run_bot(transport, from_number=from_number, **overrides)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()

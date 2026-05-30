#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tetris Nutrition Coach — Pipecat Cloud deploy entry (our build).

Defines the Nemotron service stack and hands the core a ``build_services``
callback. Pipecat Cloud's base image runs this file (``bot.py``).

Pipeline: Nemotron Speech Streaming STT -> Nemotron-3-Super-120B LLM -> Gradium TTS.

Run locally:  uv run bot.py
"""

import os

from dotenv import load_dotenv
from pipecat.runner.types import RunnerArguments
from pipecat.services.gradium.tts import GradiumTTSService

from core import bot_entry
from nemotron_llm import VLLMOpenAILLMService
from nvidia_stt import NVidiaWebSocketSTTService

load_dotenv(override=True)


def build_services(system_instruction: str):
    """Construct the Nemotron STT / LLM / Gradium TTS stack, persona baked into the
    LLM's system instruction. Endpoint URLs + keys come from the deploy secret set."""

    # STT — Nemotron Speech Streaming over WebSocket (16-bit PCM, 16 kHz, mono).
    stt = NVidiaWebSocketSTTService(
        url=os.getenv("NVIDIA_ASR_URL", "ws://192.168.7.228:8081"),
        strip_interim_prefix=True,
    )

    # LLM — Nemotron-3-Super-120B via vLLM (OpenAI-compatible). Thinking OFF for
    # low-latency voice unless the server runs a reasoning parser.
    enable_thinking = os.getenv("NEMOTRON_ENABLE_THINKING", "false").lower() == "true"
    llm = VLLMOpenAILLMService(
        api_key=os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY"),
        base_url=os.getenv("NEMOTRON_LLM_URL", "http://192.168.7.228:8000/v1"),
        settings=VLLMOpenAILLMService.Settings(
            model=os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
            system_instruction=system_instruction,
            extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": enable_thinking}}},
        ),
    )

    # TTS — Gradium.
    tts = GradiumTTSService(
        api_key=os.environ["GRADIUM_API_KEY"],
        settings=GradiumTTSService.Settings(
            voice=os.getenv("GRADIUM_VOICE_ID", "Eu9iL_CYe8N-Gkx_"),
        ),
    )

    return stt, llm, tts


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""
    await bot_entry(runner_args, build_services)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()

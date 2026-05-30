#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tetris Nutrition Coach — Nemotron deploy target (Dockerfile entry).

Thin shim: defines the Nemotron service stack and hands the shared core a
``build_services`` callback. All agent logic lives in ``coach_core.py`` /
``coach_prompt.py`` / ``nutrition_backend.py``.

Pipeline: Nemotron Speech Streaming STT → Nemotron-3-Super-120B LLM → Gradium TTS.

Run the bot using::

    uv run bot.py
"""

import os

from dotenv import load_dotenv
from pipecat.runner.types import RunnerArguments
from pipecat.services.gradium.tts import GradiumTTSService

from coach_core import bot_entry
from nemotron_llm import VLLMOpenAILLMService
from nvidia_stt import NVidiaWebSocketSTTService

load_dotenv(override=True)


def build_services(system_instruction: str):
    """Construct the Nemotron STT / LLM / Gradium TTS stack with the persona
    baked into the LLM's system instruction."""

    # Speech-to-Text — Nemotron Speech Streaming STT over WebSocket. Expects
    # 16-bit PCM, 16 kHz, mono. Override the URL via NVIDIA_ASR_URL.
    stt = NVidiaWebSocketSTTService(
        url=os.getenv("NVIDIA_ASR_URL", "ws://192.168.7.228:8081"),
        strip_interim_prefix=True,
    )

    # LLM — Nemotron-3-Super-120B served by vLLM (OpenAI-compatible /v1 chat
    # completions). Reasoning ("thinking") defaults OFF for low-latency voice;
    # set NEMOTRON_ENABLE_THINKING=true only if the server runs a reasoning
    # parser, else chain-of-thought leaks into spoken `content`. VLLMOpenAILLMService
    # reports TTFB to the first non-thinking token. See nemotron_llm.py.
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

    # Text-to-Speech
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

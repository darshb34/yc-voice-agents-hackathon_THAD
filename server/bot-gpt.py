#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tetris Nutrition Coach — GPT dev fallback.

Thin shim mirroring bot.py but with the OpenAI stack, so we can develop and A/B
in Cekura when the NVIDIA fleet is unreachable. Same agent logic and persona —
only the STT/LLM services differ (Gradium STT + OpenAI Responses LLM).

Pipeline: Gradium STT → OpenAI Responses LLM → Gradium TTS.

Run the bot using::

    uv run bot-gpt.py
"""

import os

from dotenv import load_dotenv
from pipecat.runner.types import RunnerArguments
from pipecat.services.gradium.stt import GradiumSTTService
from pipecat.services.gradium.tts import GradiumTTSService
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService
from pipecat.transcriptions.language import Language

from coach_core import bot_entry

load_dotenv(override=True)


def build_services(system_instruction: str):
    """Construct the GPT dev-fallback stack (Gradium STT + OpenAI Responses LLM +
    Gradium TTS) with the persona baked into the LLM's system instruction."""

    # Speech-to-Text
    stt = GradiumSTTService(
        api_key=os.environ["GRADIUM_API_KEY"],
        settings=GradiumSTTService.Settings(
            language=Language.EN,
        ),
    )

    # LLM
    llm = OpenAIResponsesLLMService(
        api_key=os.environ["OPENAI_API_KEY"],
        settings=OpenAIResponsesLLMService.Settings(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1"),
            system_instruction=system_instruction,
        ),
    )

    # Text-to-Speech
    tts = GradiumTTSService(
        api_key=os.environ["GRADIUM_API_KEY"],
        settings=GradiumTTSService.Settings(
            voice=os.getenv("GRADIUM_VOICE_ID", "_6Aslh2DxfmnRLmP"),
        ),
    )

    return stt, llm, tts


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""
    await bot_entry(runner_args, build_services)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()

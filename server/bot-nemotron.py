#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tetris Nutrition Coach — Nemotron (compatibility entrypoint).

The agent moved to the shared-module architecture: all logic lives in
``coach_core.py`` / ``coach_prompt.py`` / ``nutrition_backend.py``, and the
Nemotron service stack is defined in ``bot.py`` (the Dockerfile deploy target).

This file is kept only so the old ``uv run bot-nemotron.py`` command still
launches the coach. It re-exports ``bot`` from ``bot.py`` — there is no
flower-shop logic here anymore. Prefer ``uv run bot.py``.
"""

from bot import bot  # noqa: F401  (re-exported as the runner entry point)

if __name__ == "__main__":
    from pipecat.runner.run import main

    main()

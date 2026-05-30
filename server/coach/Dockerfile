FROM dailyco/pipecat-base:latest

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1
# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy
# Single-user state lives on an always-writable path (container FS is ephemeral;
# fine for a demo session — durable persistence is the swap-to-DB point).
ENV COACH_DATA_DIR=/tmp/coach_state

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Application code — Pipecat Cloud's base image runs bot.py.
COPY ./bot.py bot.py
COPY ./core.py core.py
COPY ./prompt.py prompt.py
COPY ./catalog.py catalog.py
COPY ./nutrition.py nutrition.py
COPY ./store.py store.py
COPY ./nemotron_llm.py nemotron_llm.py
COPY ./nvidia_stt.py nvidia_stt.py
COPY ./data ./data

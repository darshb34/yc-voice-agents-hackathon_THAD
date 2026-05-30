FROM dailyco/pipecat-base:latest

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Uncomment if you wish to print a summary of the features available in the base image
# ENV PCC_LOG_FEATURES_SUMMARY=true

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Copy the application code.
# Pipecat Cloud's base image runs `bot.py` (the Nemotron service-stack shim that
# hands build_services to coach_core.bot_entry). Ship the full coach package.
COPY ./bot.py bot.py
COPY ./coach_core.py coach_core.py
COPY ./coach_prompt.py coach_prompt.py
COPY ./member_store.py member_store.py
COPY ./nutrition_backend.py nutrition_backend.py
COPY ./nemotron_llm.py nemotron_llm.py
COPY ./nvidia_stt.py nvidia_stt.py
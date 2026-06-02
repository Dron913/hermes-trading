FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

COPY pyproject.toml ./
COPY hermes_trading ./hermes_trading
COPY defaults ./defaults
COPY hermes_config/ ./hermes_config/
COPY audit_self_learning.py ./

# Install project dependencies and create .venv
RUN uv sync

# Install Hermes agent — self-improving AI brain
# Binary at /app/.venv/bin/hermes (project venv)
RUN uv pip install hermes-agent

ENV HERMES_HOME=/app/hermes_config
ENV HERMES_TRADING_MODE=paper
ENV HERMES_TRADING_I_ACCEPT_RISK=false
# HERMES_REFLECTION_MODE is set in Railway project variables

# =============================================================================
# Persistence architecture:
#   /app/defaults/    ← baked into image (starting state only)
#   /app/state/       ← Railway persistent volume mount (survives redeployments)
#   Volume is seeded from defaults/ on FIRST deploy; reused on all subsequent.
#
# On Railway:
#   - Volume: tradeforge-volume, mount path /app/state/
#   - First deploy: volume empty → bootstrap seeds from defaults
#   - Subsequent deploys: volume has data → used as-is (learning persists)
#   - Rollback: volume retains prior state (no divergence from image)
# =============================================================================

# Bootstrap: seed persistent volume, audit self-learning system, then start trading
# All stdout from bootstrap+audit captured in Railway logs on every container start
CMD uv run python -m hermes_trading.bootstrap && \
    uv run python audit_self_learning.py && \
    uv run python -m hermes_trading.run
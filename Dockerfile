FROM python:3.13-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application (mobile apps / docs / tests are excluded via .dockerignore).
COPY maya ./maya
COPY README.md ./

ENV PYTHONUNBUFFERED=1

# Runs the agent. Configuration comes from the environment / .env (see
# .env.example). Override the command for other entrypoints, e.g.:
#   docker compose run --rm maya python -m maya.check_connection
#   docker compose run --rm maya python -m maya.backtest.runner
#   docker compose run --rm maya python -m maya.scanner
ENTRYPOINT ["python", "-m", "maya"]

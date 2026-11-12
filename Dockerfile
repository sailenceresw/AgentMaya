FROM python:3.13-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application.
COPY maya ./maya
COPY README.md ./

ENV PYTHONUNBUFFERED=1

# Runs the agent. Configuration comes from the environment / .env (see
# .env.example). Override the command for other entrypoints, e.g.:
#   docker run --env-file .env maya python -m maya --check
#   docker run --env-file .env maya python -m maya.backtest --source hyperliquid --days 365
ENTRYPOINT ["python", "-m", "maya"]

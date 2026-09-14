FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Persistent volumes for state + logs
RUN mkdir -p /app/data /app/logs
VOLUME ["/app/config", "/app/data", "/app/logs"]

# Dry-run by default; override with `command: ["python","-m","src.main","--config","config/config.yaml"]`
CMD ["python", "-m", "src.main", "--dry-run"]

ENV PYTHONUNBUFFERED=1

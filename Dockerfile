# TripBuddy Dockerfile
FROM python:3.11-slim

# Prevent Python from buffering stdout/stderr and bytecode generation
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency specifications and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and pre-built frontend dist
COPY tripbuddy/ ./tripbuddy/
COPY frontend/dist/ ./frontend/dist/
COPY .env.example .env

# Default command (overridden by docker-compose)
EXPOSE 8000 8100
CMD ["python", "-m", "tripbuddy.mcp_server.server"]

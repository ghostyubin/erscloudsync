# BauduSync - Multi-arch Docker Image
# Supports: linux/amd64 (x86_64) and linux/arm64 (RK3576/ARM)
#
# Build: docker buildx build --platform linux/amd64,linux/arm64 -t baudusync:latest .

FROM python:3.11-slim AS base

# Install system dependencies
# Runtime deps: libffi for cryptography, no build tools needed in final image
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libffi8 \
        libssl3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (CACHE_BUST arg forces fresh copy on rebuild)
ARG CACHE_BUST=1
COPY app/ ./app/
COPY frontend/ ./frontend/

# Create directories
RUN mkdir -p /config /sync /downloads

# Set environment
ENV BAUDUSYNC_DATA_DIR=/config
ENV BAUDUSYNC_SYNC_ROOT=/sync
ENV BAUDUSYNC_DOWNLOAD_DIR=/downloads
ENV BAUDUSYNC_HOST=0.0.0.0
ENV BAUDUSYNC_PORT=5566
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Expose web UI port
EXPOSE 5566

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5566/api/system/health')" || exit 1

# Run the application
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5566", "--workers", "1"]

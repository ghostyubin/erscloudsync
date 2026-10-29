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
# 在部分网络环境里 files.pythonhosted.org 只有几十 KB/s，一轮 pip install 要十几
# 分钟。需要时用国内源构建：
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# 默认仍走官方源，公开镜像的行为不变。
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt

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

# Run the application.
# --log-level warning quiets uvicorn's access log; the frontend polls every 3s
# and would otherwise flood logs with "200 OK" lines.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5566", "--workers", "1", "--log-level", "warning"]

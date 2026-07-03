FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NVIDIA_VISIBLE_DEVICES=void \
    CUDA_VISIBLE_DEVICES=

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir faster-whisper==1.1.1 requests

WORKDIR /app
CMD ["python", "-u", "openclaw_whisper_service.py"]

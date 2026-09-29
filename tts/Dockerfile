FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NVIDIA_VISIBLE_DEVICES=void \
    CUDA_VISIBLE_DEVICES=

# espeak-ng: Kokoro's fallback for words its own lexicon doesn't know.
RUN apt-get update \
    && apt-get install -y --no-install-recommends espeak-ng ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# CPU-only PyTorch (the GPU stays with vLLM), then Kokoro and the Opus encoder.
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
        torch kokoro==0.9.4 av==13.1.0 numpy \
    && python -m spacy download en_core_web_sm

WORKDIR /app
CMD ["python", "-u", "openclaw_tts_service.py"]

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# pypdfium2 + Pillow render scanned PDF pages for the vision model to read.
RUN pip install --no-cache-dir pypdf==5.9.0 pypdfium2 pillow

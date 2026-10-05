# The verification sandbox (bin/verify): the bots' Python plus what the
# memory watcher uses for PDFs and images, YAML for the scenarios, and the
# unit-test tools. The code under test is mounted, never baked in.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir pypdf==5.9.0 pypdfium2 pillow pyyaml pytest ruff

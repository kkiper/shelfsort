FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8585 \
    SHELFSORT_DB=/config/shelfsort.db \
    SHELFSORT_SOURCES_ROOT=/sources \
    HOME=/config \
    QT_QPA_PLATFORM=offscreen \
    PATH=/opt/calibre:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates wget \
        tesseract-ocr tesseract-ocr-eng xz-utils \
        libgl1 libegl1 libopengl0 libxcb-cursor0 libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

# Calibre via the official installer (provides ebook-meta, fetch-ebook-metadata).
RUN wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh \
    | sh /dev/stdin install_dir=/opt isolated=y

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
# Non-editable install; templates/static ship as package data. Dev extras so
# `pytest -m tools` can run inside the container.
RUN pip install ".[dev]"

# Tests (for `pytest -m tools` in the container).
COPY tests ./tests

# 99:100 = nobody:users, Unraid's convention.
RUN mkdir -p /config /sources && chown -R 99:100 /config /sources /app
USER 99:100
VOLUME /config
EXPOSE 8585

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ.get('PORT','8585'), timeout=4)"]

CMD ["sh", "-c", "exec uvicorn shelfsort.web.app:app --host 0.0.0.0 --port ${PORT:-8585}"]

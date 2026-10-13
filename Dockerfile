FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8585 \
    SHELFSORT_DB=/config/shelfsort.db \
    SHELFSORT_SOURCES_ROOT=/sources \
    HOME=/config \
    USER=shelfsort \
    LOGNAME=shelfsort \
    QT_QPA_PLATFORM=offscreen \
    PATH=/opt/calibre:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates wget \
        tesseract-ocr tesseract-ocr-eng xz-utils \
        libgl1 libegl1 libopengl0 libxcb-cursor0 libfontconfig1 \
        libxkbcommon0 libxkbcommon-x11-0 libdbus-1-3 libglib2.0-0 libnss3 \
        libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2 libxtst6 \
        libxkbfile1 \
    && rm -rf /var/lib/apt/lists/*

# Calibre via the official installer (provides ebook-meta, fetch-ebook-metadata).
# Optional: --build-arg CALIBRE_VERSION=x.y.z to pin a version.
ARG CALIBRE_VERSION=
RUN wget -nv -O /tmp/calibre-install.sh https://download.calibre-ebook.com/linux-installer.sh \
    && sh /tmp/calibre-install.sh install_dir=/opt isolated=y ${CALIBRE_VERSION:+version=$CALIBRE_VERSION} \
    && rm /tmp/calibre-install.sh \
    && ebook-meta --version && fetch-ebook-metadata --version

# Fail the build if Calibre's bundled Qt lacks a system library. Calibre ships
# some libraries (libiconv, ICU) in /opt/calibre/lib and finds them via its
# executables' RUNPATH, so put that directory on the search path for ldd.
RUN if [ -f /opt/calibre/lib/libQt6Gui.so.6 ]; then \
        if LD_LIBRARY_PATH=/opt/calibre/lib ldd /opt/calibre/lib/libQt6Gui.so.6 | grep "not found"; then \
            echo "missing libraries for Calibre Qt6" >&2; exit 1; \
        fi; \
    else echo "WARNING: /opt/calibre/lib/libQt6Gui.so.6 not found; skipping ldd check" >&2; fi

# Passwd entry for uid 99 (nobody:users) so tools that look up the user work.
RUN getent passwd 99 >/dev/null \
    || useradd -u 99 -g 100 -d /config -M -s /usr/sbin/nologin shelfsort

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
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8585'), timeout=4)"]

CMD ["sh", "-c", "exec uvicorn shelfsort.web.app:app --host 0.0.0.0 --port ${PORT:-8585}"]

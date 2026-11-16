FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    DISPLAY=:99 \
    ENABLE_VNC=1 \
    BROWSER_HEADLESS=0 \
    PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    fluxbox \
    x11vnc \
    novnc \
    websockify \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY bundled bundled
COPY static static
COPY run.py .
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

RUN mkdir -p /app/data/chrome-profile

EXPOSE 8000 6080

VOLUME ["/app/data"]

ENTRYPOINT ["/docker-entrypoint.sh"]

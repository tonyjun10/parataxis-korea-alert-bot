FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 \
    fonts-liberation fonts-noto-cjk ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# PLAYWRIGHT_BROWSERS_PATH pins the install location so the runtime
# env var and the build-time install always agree on the same path.
ENV PLAYWRIGHT_BROWSERS_PATH=/pw-browsers

# Cache-bust: changing this value forces Docker to re-run the install
# even if requirements.txt hasn't changed.
ARG PLAYWRIGHT_CACHE_BUST=2
RUN python -m playwright install chromium
RUN python -m playwright install-deps chromium

COPY src/ ./src/
RUN mkdir -p data logs

CMD ["python", "src/main.py"]

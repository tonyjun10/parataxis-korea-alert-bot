FROM python:3.12-slim

# System libs Chromium needs — installed once, cached as a layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 \
    fonts-liberation fonts-noto-cjk ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps — separate layer so code changes don't reinstall everything
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium using the SAME python that pip just installed into.
# This is the only way to guarantee the browser lands where Playwright
# will look for it at runtime (/usr/local/lib/python3.12/site-packages/...).
RUN python -m playwright install chromium

COPY src/ ./src/

RUN mkdir -p data logs

CMD ["python", "src/main.py"]

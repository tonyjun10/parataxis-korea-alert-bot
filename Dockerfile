FROM python:3.12-slim

# Install system Chromium via apt — no Playwright browser download needed.
# System Chromium is always at /usr/bin/chromium and is guaranteed to be
# present after this step regardless of any env var or path issues.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-liberation \
    fonts-noto-cjk \
    ca-certificates \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
RUN mkdir -p data logs

CMD ["python", "src/main.py"]

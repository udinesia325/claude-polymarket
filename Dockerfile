FROM python:3.12-slim

# System deps needed by lxml / web3
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code (env files are excluded via .dockerignore)
COPY . .

# TRADING_MODE is injected at runtime (fake or real)
ENV TRADING_MODE=fake

CMD ["python", "main.py"]

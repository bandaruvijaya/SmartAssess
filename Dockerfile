FROM python:3.11-slim

WORKDIR /app

# =========================
# System dependencies (REQUIRED for FAISS & torch)
# =========================
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    wget \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

# =========================
# Copy requirements
# =========================
COPY requirements.txt .

# =========================
# Install Python dependencies (CPU-only torch)
# =========================
RUN pip install --upgrade pip && \
    pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    -r requirements.txt

# =========================
# Copy application code
# =========================
COPY . .

# =========================
# Ensure SQLite directory exists
# =========================
RUN mkdir -p /app/data

# =========================
# Environment variables
# =========================
ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production

# ⚠️ DO NOT hardcode PORT
# Railway injects PORT automatically

# =========================
# Health check
# =========================
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:$PORT/health || exit 1

# =========================
# Run application
# =========================
CMD ["bash", "start.sh"]

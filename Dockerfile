# Use slim official Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system build dependencies and runtime libraries for ONNX & image processing:
# - gcc: C compiler for native extensions
# - libgl1 & libglib2.0-0: required for OpenCV / Pillow image pipelines
# - libgomp1: OpenMP runtime required by ONNX Runtime on Linux
# - curl: health check probes
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create data directory for persistent local fallback storage
RUN mkdir -p /app/data

# Copy application source code
COPY . .

# Run the telegram bot
CMD ["python", "bot.py"]

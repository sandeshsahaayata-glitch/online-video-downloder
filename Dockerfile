FROM python:3.11-slim

# Install system dependencies: ffmpeg and nodejs (for yt-dlp JS challenge solver)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application code
COPY . .

# Environment config
ENV PYTHONUNBUFFERED=1
ENV PORT=5000
EXPOSE 5000

# Run with gunicorn
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 180"]

FROM python:3.12-slim

WORKDIR /app

# Install git (required for GitPython and repository operations) and build utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Environment defaults for production container
ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    HOST=0.0.0.0 \
    DEMO_MODE=true

EXPOSE 8000

# Start command binds dynamically to assigned PaaS PORT
CMD ["sh", "-c", "uvicorn ui.server:app --host 0.0.0.0 --port ${PORT:-8000}"]

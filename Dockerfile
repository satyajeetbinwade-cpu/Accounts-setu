FROM python:3.12-slim

# Install Node.js 22 (required by Reflex to build frontend)
RUN apt-get update && apt-get install -y curl unzip && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire app
COPY . .

# Build the Reflex frontend
RUN reflex export --frontend-only --no-zip || true

# Expose backend port (frontend is served by the backend)
EXPOSE 8000

# Run in production mode
CMD ["reflex", "run", "--backend-host", "0.0.0.0"]
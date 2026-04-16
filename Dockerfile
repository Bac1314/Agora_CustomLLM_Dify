FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and config
COPY app/ ./app/
COPY config/ ./config/

# Non-root user for security
RUN useradd -r -u 1001 appuser
USER appuser

EXPOSE 8000

# PORT env var override lets platforms like Render/Railway inject their port
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

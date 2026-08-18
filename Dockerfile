FROM python:3.12.11-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

RUN addgroup --system app && adduser --system --ingroup app app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f\"http://localhost:{os.getenv('PORT', '8000')}/health\")" || exit 1

USER app

CMD ["python", "server.py"]

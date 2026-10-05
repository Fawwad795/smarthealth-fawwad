# One image, several roles. The same image will later run the API, the
# Temporal worker, the Celery worker and the Kafka consumer — only the
# `command:` in docker-compose.yml differs.

FROM python:3.11-slim

# Keep Python from writing .pyc files, and make its output unbuffered so
# log lines appear immediately in `docker compose logs` instead of being
# held in a buffer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy requirements first, install, THEN copy the source. Docker caches
# each step; because requirements.txt changes rarely, editing a .py file
# does not trigger a full re-install on rebuild.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fetch the Temporal test-server binary now rather than letting the SDK
# download it inside the first test that needs one. Copied on its own
# line instead of relying on the COPY below, so this layer caches next to
# the pip install and only re-runs when requirements.txt changes.
COPY scripts/fetch_test_server.py scripts/
RUN python scripts/fetch_test_server.py

# Where the tests look for it. Set here because it describes this image,
# not a per-machine setting -- nobody chooses this value, so it does not
# belong in .env.
ENV TEMPORAL_TEST_SERVER_PATH=/opt/temporal/temporal-test-server

COPY . .

EXPOSE 8000

# A default command. docker-compose.yml overrides it per service.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

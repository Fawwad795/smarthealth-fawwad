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

COPY . .

EXPOSE 8000

# A default command. docker-compose.yml overrides it per service.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

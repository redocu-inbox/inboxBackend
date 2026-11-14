FROM python:3.12-slim

WORKDIR /app

# Don't write .pyc files, and flush logs straight to Docker's log collector.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
# tzdata is in requirements.txt. This image is python:3.12-slim, which does not
# ship zoneinfo, and scoring resolves today/tomorrow in America/New_York.
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Default process is the API only, without --reload. Local Compose overrides
# this. The worker is a second process. The optional one-container demo is
# `python -m app.deploy.demo` and is not the default.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

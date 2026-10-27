FROM python:3.12-slim

WORKDIR /app

# Don't write .pyc files, and flush logs straight to Docker's log collector.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Compose overrides this for the API (--reload) and the worker (arq).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

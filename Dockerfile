FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FEATURE_STORE_DATABASE_PATH=/data/features.db

WORKDIR /app
COPY pyproject.toml ./
COPY feature_store.py ./
COPY app ./app
COPY storage ./storage
RUN pip install --no-cache-dir .

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]

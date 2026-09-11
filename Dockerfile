FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml feature_store.py ./
COPY app ./app
COPY feature_platform ./feature_platform
COPY storage ./storage
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FEATURE_STORE_DATABASE_PATH=/data/features.db

RUN addgroup --system feature && adduser --system --ingroup feature feature
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -r /wheels

WORKDIR /app
RUN mkdir -p /data && chown feature:feature /data
USER feature
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]

# Digest resolved from the official Docker Hub registry for the declared tag.
FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
COPY . .
RUN mkdir -p /app/data && useradd --create-home --uid 10001 botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "main.py"]

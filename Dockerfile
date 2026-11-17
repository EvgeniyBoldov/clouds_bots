FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY alembic ./alembic
COPY src ./src
RUN pip install --no-cache-dir .
ENV PYTHONPATH=/app/src
CMD ["/bin/sh", "-c", "alembic upgrade head && uvicorn gateway.main:app --host 0.0.0.0 --port 8000"]

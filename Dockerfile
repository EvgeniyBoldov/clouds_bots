ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
ARG RELEASE_VERSION
ARG SOURCE_COMMIT
LABEL org.opencontainers.image.version="${RELEASE_VERSION}" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}"
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY static ./static
RUN pip install --no-cache-dir .
ENV PYTHONPATH=/app/src
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]

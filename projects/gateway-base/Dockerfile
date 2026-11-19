FROM python:3.12-slim
ARG RUNTIME_BASE_VERSION
ARG RUNTIME_BASE_INPUT_SHA256
LABEL org.opencontainers.image.version="${RUNTIME_BASE_VERSION}" \
      com.clouds-bots.runtime-input-sha256="${RUNTIME_BASE_INPUT_SHA256}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml /tmp/pyproject.toml
RUN python -c "import tomllib; p=tomllib.load(open('/tmp/pyproject.toml','rb')); open('/tmp/requirements.txt','w').write('\n'.join(p['project']['dependencies']))" && pip install -r /tmp/requirements.txt

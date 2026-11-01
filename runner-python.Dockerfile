FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends make build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.11.12 build==1.3.0 hatchling==1.27.0 \
    && useradd --uid 1000 --create-home runner \
    && mkdir -p /workspace \
    && chown 1000:1000 /workspace
ENV HOME=/tmp/home CI=true UV_PYTHON_DOWNLOADS=never
WORKDIR /workspace
USER 1000:1000

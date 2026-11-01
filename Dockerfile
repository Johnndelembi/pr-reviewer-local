ARG PYTHON_IMAGE=python:3.12-slim-trixie
FROM ${PYTHON_IMAGE} AS runtime
# Use HTTPS with bounded retries for public package downloads.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=30 install -y --no-install-recommends ca-certificates git gh docker-cli \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home reviewer \
    && mkdir /app /data /config \
    && chown reviewer:reviewer /data /config
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Explicit files prevent tokens, personal state, workspaces and logs entering image layers.
COPY reviewer.py autofixer.py review_policy.py build_checks.py check_sandbox.py \
     companion_context.py submodule_context.py validation_plan.py sms_notifier.py \
     runtime_config.py ai_runtime.py worker.py ./
COPY review-prompt.md review-schema.json autofix-prompt.md autofix-schema.json config.example.json LICENSE ./
COPY skills/ ./skills/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    REVIEWER_CONFIG=/config/config.json REVIEWER_DATA_DIR=/data \
    HOME=/home/reviewer GH_CONFIG_DIR=/tmp/gh GH_PROMPT_DISABLED=1 GH_NO_UPDATE_NOTIFIER=1 GIT_TERMINAL_PROMPT=0
USER 1000:1000
ENTRYPOINT ["python3", "-B", "/app/worker.py"]
CMD ["serve"]

FROM runtime AS test
COPY test_*.py ./
RUN env -u REVIEWER_CONFIG -u REVIEWER_DATA_DIR python3 -B -m unittest discover -q

FROM runtime AS release
ARG VERSION=0.1.2
LABEL org.opencontainers.image.title="PR Reviewer" \
      org.opencontainers.image.description="Configurable GitHub PR reviews with bounded AI file tools" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"

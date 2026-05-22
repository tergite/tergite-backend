FROM python:3.12-slim-bookworm

WORKDIR /code

COPY pyproject.toml uv.lock* setup.py /code/

ARG DEPS_GROUP="quantify"

# Install dependencies for recalibration
RUN  if [ "$DEPS_GROUP" = "quantify" ]; then \
        apt-get update && \
        apt-get install -y --no-install-recommends  python3-pyqt5 libgl1 && \
        rm -rf /var/lib/apt/lists/*; \
    fi

# Install uv
ENV PIP_ROOT_USER_ACTION=ignore
RUN pip install --no-cache-dir uv

# Install prod-only dependencies in system python's packages
ENV UV_PROJECT_ENVIRONMENT="/usr/local"
ENV UV_NO_DEV=1
RUN uv sync --no-python-downloads --python-preference only-system --extra "$DEPS_GROUP"

# uninstall uv
RUN pip uninstall uv -y && \
    apt-get purge -y --auto-remove && \
    rm -rf /root/.cache/uv /root/.cache/pip

COPY . /code/
RUN chmod +x /code/start_bcc.sh

LABEL org.opencontainers.image.licenses=APACHE-2.0
LABEL org.opencontainers.image.description="The Backend in the Tergite software stack of the WACQT quantum computer."

# Check the dot-env-template.txt for more information about the env variables
ENV ENV_FILE=".env"
ENV IS_SYSTEMD="false"
ENV BACKEND_SETTINGS="backend_config.toml"
ENV DEFAULT_PREFIX="default"
ENV STORAGE_ROOT="/tmp"
ENV LOGFILE_DOWNLOAD_POOL_DIRNAME="logfile_download_pool"
ENV LOGFILE_UPLOAD_POOL_DIRNAME="logfile_upload_pool"
ENV JOB_UPLOAD_POOL_DIRNAME="job_upload_pool"
ENV JOB_PRE_PROC_POOL_DIRNAME="job_preproc_pool"
ENV EXECUTOR_DATA_DIRNAME="executor_data"
ENV BCC_MACHINE_ROOT_URL="http://host.docker.internal:8000"
ENV BCC_PORT=8000
ENV MSS_MACHINE_ROOT_URL="http://host.docker.internal:8002"
ENV EXECUTOR_TYPE="quantify"
ENV CALIBRATION_SEED="calibration.seed.toml"
ENV QUANTIFY_CONFIG_FILE="quantify-config.json"
ENV QUANTIFY_METADATA_FILE="quantify-metadata.yml"

#ENV MSS_APP_TOKEN=""
ENV APP_SETTINGS="production"
#ENV REDIS_HOST="host.docker.internal"
#ENV REDIS_PORT=6379
#ENV REDIS_USER=""
#ENV REDIS_PASSWORD=""
ENV MSS_PUBLIC_KEY_PATH="public-mss-key.pem"
ENV MSS_NONCE_TTL=300
# ENV JWT_SECRET=""
# ENV CORS_ORIGINS="127.0.0.1,localhost"
# ENV VAULT_ADDR
# ENV VAULT_TOKEN

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=10 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:$BCC_PORT/docs', timeout=3)" || exit 1

ENTRYPOINT ["/code/start_bcc.sh"]
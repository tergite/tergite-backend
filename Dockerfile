FROM python:3.12-bullseye

WORKDIR /code

COPY . /code/

ARG DEPS_GROUP="quantify"

# Install dependencies for recalibration
RUN  if [ "$DEPS_GROUP" = "quantify" ]; then \
      apt-get update && apt-get install libgl1 -y \
      && rm -rf /var/lib/apt/lists/* \
    fi

# Install uv
RUN pip install uv

# Install dependencies
RUN uv sync --system --extra ${DEPS_GROUP}

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

ENTRYPOINT ["/code/start_bcc.sh"]
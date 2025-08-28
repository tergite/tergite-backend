#!/bin/bash

# Run this script as follows:
#
# - go to the root of the tergite-backend repository (this directory)
# - run ./start_bcc.sh
#

exit_with_error () {
  echo "$1"
  exit 1
}

var_or_default () {
  local var="$1"
  local default_val="$2"
  if [ -z "$var" ]; then
      echo "$default_val";
  else
    echo "$var";
  fi
}

should_be_int() {
  local val="$1";
  local msg="$2";
  [[ ! "$val" =~ ^[0-9]+$ ]]  &&  exit_with_error "$msg";
}

load_env() {
  local env_file="$1";
  if [ ! -f "$env_file" ]; then
    echo "'$env_file' not found!. Defaulting to system environment variables";
  else
    . "$env_file";
  fi
}

start_auto_env_export(){
  set -o allexport;
}

stop_auto_env_export(){
  set +o allexport;
}

printf "Starting Tergite Backend ....\n";

# enable multiprocessing for python in macOS
# See https://stackoverflow.com/questions/50168647/multiprocessing-causes-python-to-crash-and-gives-an-error-may-have-been-in-progr#answer-52230415
[[ "$(uname -s)" = "Darwin" ]] && export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

#
# Environment variables
#
start_auto_env_export;
ENV_FILE=$(var_or_default "$ENV_FILE" ".env")
load_env "$ENV_FILE";

DEFAULT_PREFIX=$(var_or_default "$DEFAULT_PREFIX" "qiskit_pulse_1q")
PORT_NUMBER=$(var_or_default "$BCC_PORT" "8000")
should_be_int "$PORT_NUMBER" "Config Error. Use BCC_PORT=<int> in the .env file.";

REDIS_HOST=$(var_or_default "$REDIS_HOST" "localhost")

REDIS_PORT=$(var_or_default "$REDIS_PORT" "6379")
should_be_int "$REDIS_PORT" "Config Error. Use REDIS_PORT=<int> in the .env file.";

REDIS_USER=$(var_or_default "$REDIS_USER" "")

REDIS_PASSWORD=$(var_or_default "$REDIS_PASSWORD" "")

REDIS_DB=$(var_or_default "$REDIS_DB" "0")
should_be_int "$REDIS_DB" "Config Error. Use REDIS_DB=<int> in the .env file.";

REDIS_URL="redis://$REDIS_USER:$REDIS_PASSWORD@$REDIS_HOST:$REDIS_PORT/$REDIS_DB";

BCC_MACHINE_ROOT_URL=$(var_or_default "$BCC_MACHINE_ROOT_URL" "http://localhost:8000")
MSS_MACHINE_ROOT_URL=$(var_or_default "$MSS_MACHINE_ROOT_URL" "http://localhost:8002")
STORAGE_ROOT=$(var_or_default "$STORAGE_ROOT" "/tmp")

stop_auto_env_export

# Just for information
printf "\nBackend '$DEFAULT_PREFIX' starting on $BCC_MACHINE_ROOT_URL...\n";
printf "Port: $PORT_NUMBER\n"
printf "MSS: $MSS_MACHINE_ROOT_URL\n"
printf "Storage: $STORAGE_ROOT\n"
printf "Redis host: redis://******@$REDIS_HOST:$REDIS_PORT/$REDIS_DB\n\n\n"

# activates the conda environment passed
conda_activate(){
  . $CONDA_BIN_PATH/activate $1
}

# If we are in systemd, activate conda environment or create it if not exists, activate it and install dependencies
if [ "$IS_SYSTEMD" = "true" ]; then
  if conda_activate ./env ; then
    echo "env activated";
  else
    conda create -y --prefix=env python=3.12 && conda_activate ./env && pip install .;
    echo "env created, activated, and dependencies installed";
  fi
fi

# NOTE: careful, this causes the script to fail silently.
# Keep below the env variable extraction procedures
set -e # exit if any step fails

# Clean start
rq empty -u "$REDIS_URL" "${DEFAULT_PREFIX}_job_registration"
rq empty -u "$REDIS_URL" "${DEFAULT_PREFIX}_job_execution"
rq empty -u "$REDIS_URL" "${DEFAULT_PREFIX}_logfile_postprocessing"
rm -fr "/tmp/${DEFAULT_PREFIX}"


# Remove old Redis keys, by their prefixes, if redis-cli is installed
# - job_supervisor
# - calibration_supervisor
# - device properties
# - post-processing results
if command -v redis-cli &> /dev/null; then
  prefixes="job_supervisor calibration_supervisor postprocessing:results: device:"
  for prefix in $prefixes
  do
      echo deleting "\"$prefix*\"" from Redis
      for key in $(redis-cli -u "$REDIS_URL" --scan --pattern "$prefix*")
      do
          redis-cli -u "$REDIS_URL" del "$key" > /dev/null
      done
  done
fi

# export PYTHONPATH="$(pwd):${PYTHONPATH}"

# Worker processes
rq worker -u "$REDIS_URL" "${DEFAULT_PREFIX}_job_registration" &
rq worker -u "$REDIS_URL" "${DEFAULT_PREFIX}_job_execution" &
rq worker -u "$REDIS_URL" "${DEFAULT_PREFIX}_logfile_postprocessing" &


# REST-API
extra_params=$([[ "$IS_SYSTEMD" = "true" ]] && echo "--proxy-headers" || echo "--reload")
python -m uvicorn --host 0.0.0.0 --port "$PORT_NUMBER" app.api:app "$extra_params"


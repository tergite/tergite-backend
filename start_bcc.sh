#!/bin/bash

# Run this script as follows:
#
# - go to the root of the tergite-backend repository (this directory)
# - run ./start_bcc.sh
#

# enable multiprocessing for python in macOS
# See https://stackoverflow.com/questions/50168647/multiprocessing-causes-python-to-crash-and-gives-an-error-may-have-been-in-progr#answer-52230415
[[ "$(uname -s)" = "Darwin" ]] && export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

env_file=$([[ -z "$ENV_FILE" ]] && echo ".env" || echo "$ENV_FILE")

exit_with_error () {
  echo "$1"
  exit 1
}

extract_env_var () {
  local env_name="$1"
  local res=$(grep "^[[:space:]]*${env_name}=" "$env_file" | grep -v '^[[:space:]]*#' | sed "s/^[[:space:]]*${env_name}=//" | head -n 1)
  [[ -z "$res" ]]  &&  exit_with_error "Config Error: Use ${env_name}=<value> in the .env file."
  echo $res
}

PORT_NUMBER=$(extract_env_var "BCC_PORT")
[[ ! "$PORT_NUMBER" =~ ^[0-9]+$ ]]  &&  exit_with_error "Config Error. Use BCC_PORT=<int> in the .env file."

DEFAULT_PREFIX=$(extract_env_var "DEFAULT_PREFIX")

# activates the conda environment passed
conda_activate(){
  . $CONDA_BIN_PATH/activate $1
}

# If we are in systemd, activate conda environment or create it if not exists, activate it and install dependencies
if [ "$IS_SYSTEMD" = "true" ]; then
  if conda_activate ./env ; then
    echo "env activated";
  else
    conda create -y --prefix=env python=3.9 && conda_activate ./env && pip install -r requirements.txt;
    echo "env created, activated, and dependencies installed";
  fi
fi

# NOTE: careful, this causes the script to fail silently.
# Keep below the env variable extraction procedures
set -e # exit if any step fails

# Clean start
rq empty "${DEFAULT_PREFIX}_job_registration"
rq empty "${DEFAULT_PREFIX}_job_preprocessing"
rq empty "${DEFAULT_PREFIX}_job_execution"
rq empty "${DEFAULT_PREFIX}_logfile_postprocessing"
rm -fr "/tmp/${DEFAULT_PREFIX}"


# Remove old Redis keys, by their prefixes
# - job_supervisor
# - calibration_supervisor
# - device properties
# - post-processing results
prefixes="job_supervisor calibration_supervisor postprocessing:results: device:"
for prefix in $prefixes
do
    echo deleting "\"$prefix*\"" from Redis
    for key in $(redis-cli --scan --pattern "$prefix*")
    do
        redis-cli del "$key" > /dev/null
    done
done


# Worker processes
rq worker "${DEFAULT_PREFIX}_job_registration" &
rq worker "${DEFAULT_PREFIX}_job_preprocessing" &
rq worker "${DEFAULT_PREFIX}_job_execution" &
rq worker "${DEFAULT_PREFIX}_logfile_postprocessing" &

# REST-API
extra_params=$([[ "$IS_SYSTEMD" = "true" ]] && echo "--proxy-headers" || echo "--reload")
python -m uvicorn --host 0.0.0.0 --port "$PORT_NUMBER" app.api:app "$extra_params"

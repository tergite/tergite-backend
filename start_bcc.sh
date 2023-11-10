#!/bin/bash

# Run this script as follows:
#
# - go to the root of the tergite-bcc rpository (this directory)
# - run ./start_bcc.sh --device [FILEPATH]
#
# where FILEPATH is a TOML file with device configuration. See
# backend_properties_config/device_test.toml for an example.
#
# For convenience, for instance do:
# ln -s FILEPATH ./device.toml
# where FILEPATH is the full path to the desired TOML file.

exit_with_error () {
  echo "$1"
  exit 1
}

extract_env_var () {
  local env_name="$1"
  local res=$(grep "^[[:space:]]*${env_name}=" .env | grep -v '^[[:space:]]*#' | sed "s/^[[:space:]]*${env_name}=//" | head -n 1)
  [[ -z "$res" ]]  &&  exit_with_error "Config Error: Use ${env_name}=<value> in the .env file."
  echo $res
}

PORT_NUMBER=$(extract_env_var "BCC_PORT")
[[ ! "$PORT_NUMBER" =~ ^[0-9]+$ ]]  &&  exit_with_error "Config Error. Use BCC_PORT=<int> in the .env file."

DEFAULT_PREFIX=$(extract_env_var "DEFAULT_PREFIX")

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

# Load configuration files, passing the command-line args to load_config
# for now load_config expects only one option: "--device DEVICE_FILE_PATH"
# other possible arguments in $@ will be passed back as "unrecognized_args",
# so they can be used if needed, in later parts of this script
unrecognized_args=$(python backend_properties_config/load_config.py "$@")

if test -n "$unrecognized_args"; then
   echo "Arguments unrecognized by load_config.py: $unrecognized_args, in case needed below"
fi

# Worker processes
rq worker "${DEFAULT_PREFIX}_job_registration" &
rq worker "${DEFAULT_PREFIX}_job_preprocessing" &
rq worker "${DEFAULT_PREFIX}_job_execution" &
rq worker "${DEFAULT_PREFIX}_logfile_postprocessing" &

# REST-API
uvicorn --host 0.0.0.0 --port "$PORT_NUMBER" rest_api:app --reload

#!/bin/bash

# Run this script as follows:
#
# - go to the root of the tergite-backend repository (this directory)
# - run ./start_bcc.sh
#

# ------------------------------------------------------------------------------
# Private Helpers:
# These start with an underscore and are not meant to be used outside this file
# ------------------------------------------------------------------------------

# Checks if the given software is installed
function _is_installed() {
    command -v "$1" >/dev/null 2>&1;
}

# exit after printing a message to the screen
_exit_with_error () {
  echo "$1"
  exit 1
}

# ensures homebrew is installed
function _ensure_brew_exists () {
  if ! _is_installed brew; then 
    echo "installing homebrew first. More details at https://brew.sh/";
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)";
  fi
  return 0;
}

# install a given package via the of the current operating system
# usage:
#   _install_via_pkg_manager <package-to-install>
function _install_via_pkg_manager() {
  local os;
  os="$(uname -s | tr '[:upper:]' '[:lower:]')";
  local package="$1";

  echo "Installing $package";

  if [ "$os" = "darwin" ]; then
    _ensure_brew_exists;
    brew install "$package";
  elif [ "$os" = "linux" ]; then
    if [ -f /etc/os-release ]; then
      # shellcheck source=/dev/null
      . /etc/os-release  # Load variables like $ID and $NAME

      case "$ID" in
        ubuntu|debian)
          apt-get install -y "$package";
          ;;
        fedora)
          dnf install -y "$package";
          ;;
        arch)
          pacman -Sy --noconfirm "$package";
          ;;
        opensuse*|sles)
          zypper install -y "$package";
          ;;
        alpine)
          apk add "$package";
          ;;
        nixos)
          nix-env -i "$package";
          ;;
        *)
          _exit_with_error "no support for unknown distro of linux $ID"
          ;;
      esac
    else 
      _exit_with_error "Unknown linux distro";
    fi
  else 
    _exit_with_error "Unsupported operating system $os";
  fi

  return 0;
}

# installs sops for the current operating system and architecture
# Usage: _install_sops <version>
function _install_sops() {
  local os;
  local raw_arch;
  local version="${1:-v3.10.2}";

  os="$(uname -s | tr '[:upper:]' '[:lower:]')";
  raw_arch="$(uname -m)";

  echo "Installing sops";

  if [ "$os" = "darwin" ]; then
    _ensure_brew_exists;
    brew install sops;
  elif [ "$os" = "linux" ]; then
    # get the right arch name from raw_arch
    case "$raw_arch" in
      x86_64) arch="amd64" ;;
      aarch64 | arm64) arch="arm64" ;;
      armv7l) arch="arm" ;;
      *) _exit_with_error "Unsupported architecture: $raw_arch" ;;
    esac

    if ! _is_installed curl; then 
      _install_via_pkg_manager curl;
    fi

    local binary_name="sops-$version.$os.$arch";
    # Download the binary
    curl -LO "https://github.com/getsops/sops/releases/download/$version/$binary_name";
    # Move the binary in to your PATH
    mv "$binary_name" /usr/local/bin/sops;

    # Make the binary executable
    chmod +x /usr/local/bin/sops;
  else 
    _exit_with_error "Unsupported operating system $os";
  fi

  return 0;
}

# Checks whether the file is encrypted
# Usage: _is_env_encrypted <file-path>
function _is_env_encrypted() {
  local file_path="$1";
  if grep "sops_hc_vault__" "$file_path" &> /dev/null; then 
    return 0; 
  else 
    return 1; 
  fi
}

# -----------------------------------------------------------------------------
# Main helpers
# -----------------------------------------------------------------------------

# reads the env file into environment, decrypting it if it is encrypted
# Usage: load_env .env
function load_env() {
  local file_path="$1";
  local sops_version="v3.10.2";

  # required software 
  if ! _is_installed sops; then 
    _install_sops "$sops_version" &> /dev/null;
  fi 

  if [ ! -f "$file_path" ]; then
    echo "'$file_path' not found!. Defaulting to system environment variables";
  elif _is_env_encrypted "$file_path"; then 
    # it picks the VAULT_TOKEN and VAULT_ADDR from the environment
    # shellcheck disable=SC1090
    . <(sops decrypt "$file_path");
  else
    # shellcheck disable=SC1090
    . "$file_path";
  fi 
}

# validates that a value should be an integer
should_be_int() {
  local val="$1";
  local msg="$2";
  [[ ! "$val" =~ ^[0-9]+$ ]]  &&  _exit_with_error "$msg";
}

# automatically exports the defined variables to the environment
start_auto_env_export(){
  set -o allexport;
}

# stops the automatic export of environment variables to the environment
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
ENV_FILE="${ENV_FILE:-".env"}"
load_env "$ENV_FILE";

DEFAULT_PREFIX="${DEFAULT_PREFIX:-qiskit_pulse_1q}";
UVICORN_LOG_LEVEL="${UVICORN_LOG_LEVEL:-info}"
PORT_NUMBER="${BCC_PORT:-8000}"
should_be_int "$PORT_NUMBER" "Config Error. Use BCC_PORT=<int> in the .env file.";

REDIS_HOST="${REDIS_HOST:-localhost}"

REDIS_PORT="${REDIS_PORT:-6379}"
should_be_int "$REDIS_PORT" "Config Error. Use REDIS_PORT=<int> in the .env file.";

REDIS_USER="${REDIS_USER:-}"

REDIS_PASSWORD="${REDIS_PASSWORD:-}"

REDIS_DB="${REDIS_DB:-0}"
should_be_int "$REDIS_DB" "Config Error. Use REDIS_DB=<int> in the .env file.";

DEFAULT_REDIS_URL="redis://$REDIS_USER:$REDIS_PASSWORD@$REDIS_HOST:$REDIS_PORT/$REDIS_DB";
REDIS_URL="${RQ_REDIS_URL:-$DEFAULT_REDIS_URL}";

BCC_MACHINE_ROOT_URL="${BCC_MACHINE_ROOT_URL:-http://localhost:8000}"
MSS_MACHINE_ROOT_URL="${MSS_MACHINE_ROOT_URL:-http://localhost:8002}"
STORAGE_ROOT="${STORAGE_ROOT:-"/tmp"}"

stop_auto_env_export

# Just for information
echo
echo "Backend '$DEFAULT_PREFIX' starting on $BCC_MACHINE_ROOT_URL...";
echo "Port: $PORT_NUMBER";
echo "MSS: $MSS_MACHINE_ROOT_URL";
echo "Storage: $STORAGE_ROOT";
echo "Redis host: redis://******@$REDIS_HOST:$REDIS_PORT/$REDIS_DB";
echo
echo

# activates the conda environment passed
conda_activate(){
  # shellcheck disable=SC1091
  . "$CONDA_BIN_PATH/activate" "$1";
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

WORKER_FLAG="";
if [[ "${DEBUG:-}" = "true" ]]; then
  WORKER_FLAG="-w app.utils.logging.LoggingRqWorker";
fi

# Worker processes
rq worker -u "$REDIS_URL" "$WORKER_FLAG" "${DEFAULT_PREFIX}_general" &
rq worker -u "$REDIS_URL" "$WORKER_FLAG" "${DEFAULT_PREFIX}_preprocessing" &
rq worker -u "$REDIS_URL" "$WORKER_FLAG" "${DEFAULT_PREFIX}_normal_execution" &
rq worker -u "$REDIS_URL" "$WORKER_FLAG" "${DEFAULT_PREFIX}_booked_execution" &
rq worker -u "$REDIS_URL" "$WORKER_FLAG" "${DEFAULT_PREFIX}_postprocessing" &

# REST-API
extra_params=$([[ "$IS_SYSTEMD" = "true" ]] && echo "--proxy-headers" || echo "--reload")

# Then run uvicorn
python -m uvicorn --host 0.0.0.0 --port "$PORT_NUMBER" --log-level "$UVICORN_LOG_LEVEL" app.api:app "$extra_params"


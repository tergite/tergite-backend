# Change Log

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project follows versions of format `{year}.{month}.{patch_number}`.

## [Unreleased]

### Added

- Added GitLab jobs that, on a `v*` tag, build the debs and macOS packages, upload them to the package registry and
  link them from the GitLab release; the macOS ones run where `PUBLISH_MACOS_ARM64` is `true`, on a runner tagged `macos`
- Added amd64 debs, for Debian 13 and Ubuntu 24.04 onwards, and macOS arm64 packages, `tergite-backend` and `tergite-backend-simulator`, running each
  backend instance, named for its `DEFAULT_PREFIX`, as a `bcc@<name>` service set up by `bcc-provision`
- Added `bcc start`, the default command, to the Nuitka binary, which runs the API, one RQ worker for preprocessing, executions and
  recalibrations, and `--light-workers` (default 1) for postprocessing and general tasks as one process tree
- Added `--env-file`, given once per device, to `bcc start`, `serve` and `rq`, which serves several devices from one
  process, each device's API under `/<DEFAULT_PREFIX>/`, with a heavy worker per device and light workers shared by all
- Added `--workers` to `bcc start` and `serve`, the number of API processes, defaulting as uvicorn does to
  `$WEB_CONCURRENCY` or 1; `BCC_ENV_FILE` may name several env files, separated as in PATH, as `--env-file` does
- Added `BCC_<NAME>` env variables, e.g. `BCC_MAX_IDLE_TIME`, which set every device's `<NAME>`, and
  `BCC_<PREFIX>_<NAME>`, e.g. `BCC_LOKE_MAX_IDLE_TIME`, one device's over them

### Changed

- Changed the settings to be read with pydantic-settings: the `BCC_` names are the same, but now match whatever their
  case, and an empty value counts as unset rather than as an empty string
- Changed the BCC to keep no password for a user: it hashed the one MSS sends and checked it nowhere, so
  `bcrypt` is no longer needed
- Moved `start_bcc.sh` to `dev/start_bcc.sh`, with the other development tooling; it runs from any folder as before
- bcc needs Python 3.13 or later, and its debs and macOS packages are built with it: on 3.12 a Nuitka build
  raised TypeError where anyio sorts exception groups, as CPython 3.12 refuses compiled functions there
- The debs and macOS packages install `bcc` as Nuitka's standalone folder in `/usr/lib/<package>`, linked from the
  PATH, instead of a onefile binary that unpacked itself into a temporary folder on every start
- Changed `start_bcc.sh` to run `main.py start`, after decrypting a SOPS env file, instead of the API and six RQ
  workers itself; it no longer installs sops, and `BCC_DEBUG` and `BCC_UVICORN_LOG_LEVEL` are gone with its uses of them
- Renamed `dot-env-template.txt` to `.env.example`, which now also documents the env variables and serving several devices
- Changed every env variable and env file key the backend reads to its `BCC_` name, e.g. `BCC_DEFAULT_PREFIX`: a bare
  one is no longer read, and in an env file is an error, and `BCC_ENV_FILE` replaces `ENV_FILE`
- Renamed the settings `BCC_PORT` and `BCC_MACHINE_ROOT_URL` to `PORT` and `MACHINE_ROOT_URL`, so their variables and
  env file keys stay `BCC_PORT` and `BCC_MACHINE_ROOT_URL`
- Changed the API to start without building a quantum executor or importing quantify, in about 2 s and 80 MB
  instead of 10 s and 330 MB; a broken quantify config or metadata file now fails the heavy worker's start instead

### Fixed

- Fixed scheduled jobs never running: `bcc start` runs its workers with RQ's scheduler, so a booking's start and
  end jobs and recalibration's start and repeats now run, which no release's workers did
- Fixed replayed MSS requests being accepted while the first was still running, or after it crashed: the nonce is
  now claimed before the route runs, instead of recorded after it
- Fixed cancelling a job leaving its rq job queued: cancellation looked for an rq job id that was never used, so the
  rq job still ran and took whatever job was next in its queue
- Fixed queue durations going negative: cancelling a job lowered the total duration of every queue it was not in,
  so the scheduler thought more time was free before a booking than there was
- Fixed the running job's time being left out of an execution queue's duration, so a job could be queued as fitting
  before a booking, or in what was left of one, when it did not
- Fixed switching one device off taking every device served from the same Redis offline; a device switched off
  before upgrading leaves every worker suspended until `rq resume` is run
- Fixed devices on one Redis sharing their jobs, so one device listed, and could cancel, another's, and sharing one
  executor lock in the working folder; jobs stored before upgrading are no longer listed
- Fixed failed jobs not reaching MSS, and some staying `executing`: a job failing in preprocessing, execution or
  postprocessing, or too long for its booking, is now marked failed and sent to MSS
- Fixed one failed recalibration ending recalibration for good, and `/recalibration/cancel` not stopping a
  recalibration that was running; the interval now runs from one recalibration's start to the next's
- Fixed finished jobs being cancellable, which turned a successful or failed job `cancelled`, and deleting a
  cancelled job failing with a 500; cancelling a finished job now answers 406
- Fixed MSS admins being refused, with 401 or 404, the jobs and bookings of a device they had not yet fetched a
  job token from
- Fixed deleting a user leaving their running jobs to finish, and leaving other users' jobs waiting on the
  waitlist until a later booking ended, when the user's booking was under way
- Fixed two API workers being able to book the same slot, and a first request for a new user, or a duplicate
  `POST /users`, failing with 500 when it raced another; a duplicate user now answers 409
- Fixed `/switch/on` and `/switch/off` freezing every other request on their API worker, for up to 120 s, while
  they waited on MSS
- Fixed `GET /recalibration/info` failing with 500 when one recalibration was queued and another scheduled
- Fixed `/recalibration/init` scheduling a `start_timestamp` without a timezone in the server's local time, not UTC
- Fixed a backend config without `online_date` getting the time the backend was started, not when it was read
- Fixed a booker's job being queued when it fit the booking's whole length but not the time left in it, and a job
  whose booking ended during its preprocessing waiting on the waitlist for ever; it now goes to the normal queue
- Fixed recalibration with several clusters in the quantify metadata recalibrating the whole device once per
  cluster and keeping the last; as tergite_tuner works through one cluster, recalibration now refuses several
- Fixed misleading status codes: an offline device now answers 503, not 404; a job that does not exist 404, not
  403; and MSS refusing or not answering a request 502 or 504, not 500
- Fixed MSS-signed requests whose `x-mss-is-admin`, method or path could be changed in transit, as only the user,
  nonce and timestamp were signed; MSS must sign the new message too, so the two are upgraded together
- Fixed every job token being expired when issued on a device whose `BCC_CURRENT_DATE` is in the past: its expiry is
  now counted from the real time, as PyJWT checks it
- Fixed a process building quantify executors for two configs failing with AttributeError on the second, which closed
  every cluster before the components cached for the first

### Removed

- Removed `bcc.service` and `start_bcc.sh`'s conda-based systemd mode, which the packages replace
- Removed the Docker image and its release jobs; the debs and macOS packages, or `bcc` from a checkout, run the backend

## [2026.09.0-rc.1] - 2026-07-03

### Changed

- Replaced the async websocket MSS connection with a blocking synchronous one, removing
  the `asyncio` event loop and `AsyncMssClient` complexity from the MSS service
- Replaced Redis PubSub inter-process communication (between FastAPI and RQ worker
  processes) with direct singleton access to shared connection objects
- Made DB, MSS, and Redis connections singletons (cached per URL) to prevent port
  exhaustion from creating a new connection on every request or job execution
- Executor is now closed immediately after each RQ task completes to avoid `TIME_WAIT`
  socket accumulation under rapid job throughput

## [2026.06.0] - 2026-06-26

This is part of the tergite release v2026.06.0.

### Added

- Added `/switch/on`, `/switch/off` and `/switch/status` to control the ON/OFF of the device
- Added recalibration offering to the scheduler service, to start on startup
- Added recalibration endpoints to the API to restart (`/recalibration/init`),  
  view (`/recalibration/info`), and cancel (`/recalibration/cancel`) recalibration.
- Added the `tergite-tuner` library as a dependency
- Added the `name` on `QubitCalibration`, `short_name`, `full_name`, `control_qubit`  
  and `target_qubit` on `CouplerCalibration`.

### Changed

- Changed to use `uv` for managing dependencies due to the complicated hierarchy of
  dependencies in this project
- Moved the 'dev' dependencies from 'optional-dependencies' to 'dependency-groups' in
  pyproject.toml
- [BREAKING] optional-dependencies under the `quantify` group now explicitly conflict
  with those under the `qiskit` group meaning one can't use the same environment for both
- [BREAKING] Separated the Docker images produced for this project to have tags like
  `2026.06.0-qiskit`, `2026.06.0-quantify`, `latest-quantify`, `latest-qiskit`
- Removed the extra dependencies in pyproject that are not explicitly used by the code
- Changed base Docker image to python:3.12-bookworm-slim in Dockerfile

### Fixed

- Fixed errors when websocket fails to connect or disconnects while the server is running
- Fixed the 'sops command not found' error on startup when run in docker with encrypted env file
- Fixed meas mode avg issue in post-processing
- Fixed experiments sorted numerically to avoid mixup

### Contributors

- Martin Ahindura

## [2026.06.0-rc.4] - 2026-05-28

### Added

- Added `/switch/on`, `/switch/off` and `/switch/status` to control the ON/OFF of the device

## [2026.06.0-rc.3] - 2026-05-25

### Fixed

- Fixed the 'sops command not found' error on startup when run in docker with encrypted env file

## [2026.06.0-rc.2] - 2026-05-21

### Changed

- Changed to use `uv` for managing dependencies due to the complicated hierachy of
  dependencies in this project
- Moved the 'dev' dependencies from 'optional-dependencies' to 'dependency-groups' in
  pyproject.toml
- [BREAKING] optional-dependencies under the `quantify` group now explicitly conflict
  with those under the `qiskit` group meaning one can't use the same environment for
  both
- [BREAKING] Separated the Docker images produced for this project to have tags like
  `2026.06.1-qiskit`, `2026.06.1-quantify`, `latest-quantify`, `latest-qiskit`
- Removed the extra dependencies in pyproject that are not explicitly used by the code.
  These were originally put there to help the pip resolver when resolving nested dependencies.
- Changed base Docker image to python:3.12-bookworm-slim in Dockerfile

### Added

- Added recalibration offering to the scheduler service, to start on startup
- Added recalibration endpoints to the API to restart (`/recalibration/init`), 
  view (`/recalibration/info`), and cancel (`/recalibration/cancel`) recalibration.
- Added the `tergite-tuner` library as a dependency
- Added the `name` on `QubitCalibration`, `short_name`, `full_name`, `control_qubit`
  and `target_qubit` on `CouplerCalibration`.

## [2026.06.0-rc.1] - 2026-05-19

### Fixed

- Fixed errors when websocket fails to connect or disconnects while the server is running
- Meas mode avg issue in post-processing fixed 
- Experiments sorted numerically to avoid mixup 

## [2026.03.2] - 2026-03-26

### Added 

- Added a lo_freqs and drive_freqs to quantify experiment and change shift phase based on them.

## [2026.03.1] - 2026-03-17

### Added

- Added rq queue default timeouts set from environment vars 
  `MAX_EXECUTION_TIME`, `MAX_PREPROCESSING_TIME`, `MAX_POSTPROCESSING_TIME` and `MAX_GENERAL_QUEUE_TIME`

### Fixed

- Add beta qobj parameters for gaussian pulse for compatibility with new SDK-level drag instruction

## [2026.03.0] - 2026-03-16

- No Change

## [2025.12.0] - 2026-03-16

### Added

- Added decryption of `.env` file in the `bcc_start.sh` script
- [BREAKING] Added the `PRIVATE_KEY_FILE`, `PRIVATE_KEY_PASSWORD` environment variables
- Added `PYTEST_ARGS` CI environment variable for use in CI
- Added SpiDAC controller for controlling the flux tunable couplers
- Added `REQUEST_LOG_TTL` and `REQUEST_LOG_CLEAN_INTERVAL` to control the rate at which request logs are cleared
- Added `JOBS_STORE_TTL` and `JOBS_STORE_CLEAN_INTERVAL` to control the rate at which old jobs data is cleared from redis store
- Added TTL on `RedisStore`
- Added motzoi parameter to the calibration config seed 
- Added clock phase resets for all drive channels before running each job

### Changed

- Removed the automatic clearing of the redis database on every startup
- Removed the JOB_EXECUTION_POOL_DIRNAME env variable
- [BREAKING] Removed the `MSS_APP_TOKEN` environment variable
- Changed connection to MSS to use RSA-secured websockets instead of RESTful HTTP requests
- Fixed issue with executor being re-initialized on every job submission
- Refactored SpiDAC code to be less dependent on global variables but to be fully dependent on the parent executor
- Replaced Gaussian pulse with DRAG pulse for quantify executor
- Changed `discriminate_results` in `quantum_job` to explicitly map quantify qubits ids to qobj qubit ids
- Changed `ShiftPhase` Instruction in Quantify to convert radians to degrees
- Changed quantify executor initialization to allow skipping cluster reset for stability

### Fixed

- Fixed 'Error 111 connecting to localhost:6379' when only RQ_REDIS_URL is provided
- Fixed KeyError 'edges' due to new version of networkx
- Fixed qobj to quantify scheduling mapping issue by adding barrier, dispatch and sync compensations in quantify executor
- Fixed SPI_Dac logic allowing to restore currents to values recorded before running a job 
- Fixed `WacqtCZPulse` Instruction for quantify backend to use SoftSquare pulse instead of numerical waveform
- Fixed `discriminate_results` in `quantum_job` scripts to properly process circuit_level acquisition requirements 

## [2025.09.1] - 2025-11-06

### Added

- Added `isort` check in the CI files
- Added `isort` in the `pre-commit-config.yaml` file
- Added `CURRENT_DATE` environment variable to mock the global current timestamp, useful during tests
- Added the `/bookings/config` endpoint to return the configuration for the booking service
- Added CURRENT_DATE environment variable for mocking global current timestamp in testing environments.
- Added filtering bookings at `/bookings` endpoint by `user_id`
- Added sorting bookings at `/bookings` endpoint by any field

### Fixed

- Fixed error "invalid value for log level" when starting BCC without specifying the `$UVICORN_LOG_LEVEL` env variable
- Fixed error failing qiskit-dynamics backend tests
- Fixed 'KeyError: 'Another instrument has the name: quantify'' in spi tests
- Fixed AttributeError: module 'bcrypt' has no attribute '**about**'
- Fixed 'SyntaxWarning: invalid escape sequence '\d''
- Fixed 'user of id ... not found' error when cancelling booking created by an MSS user

### Changed

- Changed `LOG_LEVEL` default value to 'ERROR'

## [2025.09.0] - 2025-10-02

### Added

- Install a pre-commit hook to run black
- Added a support for measurement level 1 for simulator for both single and avg modes
- Added a support for measurement level 1 for real backend for both single and avg modes
- Added authentication via JWT, with temporary JWT token generated by the backend instance
  and passed through MSS to the SDK
- Added support for booked time slots, on top of the first-in-first-out (FIFO) queue
- Added the following endpoints for the sake of handling bookings:
  - `/token` (POST)
  - `/users` (POST, GET)
  - `/users/{user_id}` (POST, GET, DELETE)
  - `/me` (DELETE, GET)
  - `/bookings` (POST, GET)
  - `/bookings/{booking_id}/cancel` (POST)
- BREAKING: Added authentication of requests from MSS by verifying that `x-mss-signature` is signed using MSS's private key
- Added canceling of jobs via MSS
- Added and adapted SpiDAC class from autocalibration package for coupler biasing
- Modify Quantify quantum executor by calling SpiDAC to bring up currents before running a schedule
- Add a placeholder Wacqt-two-qubit-gate function for Qobj to QuantifySchedule mapper with a flux port reference
- Add filtering bookings by minimum/maximum start timestamps

### Changed

- BREAKING: Changed authentication during job submission to use JWT token only
- BREAKING: Removed authentication of requests by referer's IP address
- BREAKING: Removed authentication of job-submission requests that would check that the API key
  submitted was associated with the job submitted
  - BREAKING: Removed the `/auth` endpoint
- BREAKING: Changed the return type of the `/jobs` POST endpoint to be the job in JSON as opposed to `{"message": string}`
- BREAKING: Added pagination to the `/jobs` GET endpoint, changing the return type
  to `{"data": []object, limit: number, skip: number}` as opposed to `[]object`
- BREAKING: Changed the `/jobs` endpoint to return only jobs that belong to the user whose id
  is in the `x-mss-user-is` header.
- BREAKING: Changed the return type of the `/jobs` GET endpoint to be the job in JSON as opposed to `{"message": object}`
- BREAKING: Removed the `/jobs/{job_id}/status` endpoint.
- BREAKING: Removed the `/jobs/{job_id}/result` endpoint.
- BREAKING: Changed the return type of the `/jobs` DELETE endpoint to `{"status": "success", "detail": string}`
  as opposed to `{"message": string}`
- BREAKING: Changed the return type of the `/jobs/{job_id}/cancel` POST endpoint
  to `{"status": "success", "detail": string}` as opposed to nothing
- BREAKING: Changed the return type of the `/logfiles/{logfile_id}` GET endpoint when an error
  occurs to return an error status code with `{"detail": string}` JSON response.
- BREAKING: Changed the names of the workers/queues used to `{PREFIX}_general`, `{PREFIX}_preprocessing`, `{PREFIX}_normal_execution`,
  `{PREFIX}_booked_execution`, `{PREFIX}_postprocessing`

### Fixed

- Fix bcrypt 5.0.0 ValueError 'password cannot be longer than 72 bytes, truncate manually if necessary (e.g. my_password[:72])'

## [2025.06.2] - 2025-06-17

- No change

## [2025.06.1] - 2025-06-17

- No change

## [2025.06.0] - 2025-06-16

### Added

- Add a simpler JSON document based store in redis

### Changed

- Use memory slot size from qobj in discriminate_results in quantum_job
- Changed source of lda parameters to backend's redis store. Originally they were retrieved from MSS.
- Added proper HTTP status codes for failed REST API requests
  - InvalidJobIdInUploadedFileError results in a 400 HTTP response (originally was 200)
  - ItemNotFoundError results in a 404 HTTP response (originally was 200)
  - JobAlreadyCancelled results in a 406 HTTP response (originally was 500)
- BREAKING: Change endpoint `/v2/static-properties` to `/static-properties`
- BREAKING: Change endpoint `/v2/dynamic-properties` to `/dynamic-properties`
- Rename the `properties` library to `device_parameters`
- Upgraded to python 3.12
- Removed the deprecated `qiskit-ibmq-providers` dependency, moving its code into our lib folder
- Moved from requirements.txt to pyproject.toml setup
- Bumped up the version of fastapi to the latest (>=0.115.12)
- Added validation of job files when uploading jobs to `/jobs`

## [2025.03.2] - 2025-03-19

### Changed

- Remove stale fixtures

### Fixed

- Fixed JSONDecodeError when application is run in systemd

## [2025.03.1] - 2025-03-18

### Changed

No change

## [2025.03.0] - 2025-03-18

### Changed

- Removed the storage-file internal lib
- Limited pyarrow to versions '18.0.0' and below for macOS
- Decouple data from algorithms in storage file
- Decouple native job run from executor instance
- Remove debug prints of qobj when running jobs
- Rename the `run_experiment` and `run` methods of the `QuantumEexcutor` class to `_run_native`, `run`
- Clean up qobj-to-quantify compilation
- Removed `Program`
- Removed the `dag` property of the `NativeExperiment` class
- Enchanced the `Channel` class to track all instructions attached to it
- Added the 'to_operation' method on the `BaseInstruction` of quantify, removing the long if-conditional `QauntifyExperiment.schedule_operation` method that was originally generating Operator's
- Replaced the looping of the DAG with the looping of the instructions on each channel
- Created the `ChannelRegistry` dict-like class to track the state of all channels attached to an experiment
- Deleted `rot_left` and `ceil4` utility functions and other unused utility functions
- Split `FreqInstruction` class to `SetFreqInstruction` and `ShiftFreqInstruction`
- Split `PhaseInstruction` class to `SetPhaseInstruction` and `ShiftPhaseInstruction`
- Added the `QBLOX_TIMEGRID_INTERVAL` constant
- Moved the `Instruction` class in the `quantum_executor/base` folder to `quantum_executor/quantify` folder, renaming it to `BaseInstruction`
- Removed the `channels` property from the `NativeExperiment` class
- Added the `channel_registry` property on the `QauntifyExperiment` class
- Moved the `Channel` definition from `utils` to the `quantum_executor/quantify` folder
- Removed `retworkx` from the requirements.txt
- Updated quantify-scheduler, quantify-core and qblox-instruments and pydantic versions
- BREAKING CHANGE: Split cluster configurations into `quantify-config.json` and `quantify-metadata.json`
- BREAKING CHANGE: Added a new configuration file `calibration.seed.toml` for seeding the database with calibration data
- Enhanced the `QuantifySchedule` conversion to use parametric schedules and new portclock convention

## [2024.12.2] - 2024-12-23

### Changed

- Changed the apt installation step in Dockerfile to remove `/var/lib/apt/lists/*` after completion
- Made removal of keys in `start_bcc.sh` optional

### Fixed

- Fixed docker error ModuleNotFoundError: No module named 'setuptools.command.build'

## [2024.12.1] - 2024-12-20

### Added

- Added Dockerfile.
- Added instructions how to run with docker.
- Added instructions how to run the qiskit_pulse_2q simulator in the configuration docs.

### Changed

- Changed `start_bcc.sh` script to use redis connection obtained from the environment.
- Changed `start_bcc.sh` script to update the exported environment variables after reading from the `ENV_FILE`.
- Updated Github action to deploy built multiplatform image to docker hub as something like `tergite/tergite-backend`
- Removed some redundant libraries in the `requirements.txt` file.
- Removed some outdated docs.

## [2024.12.0] - 2024-12-13

### Added

- Added redis connection environment variables
- Added GitLab CI configuration
- Added storing of Qobj header data in the logfiles of the quantum jobs
- Qiskit dynamics simulator backend with two-qubit CZ gate ("qiskit_pulse_2q")
- Added CouplerProps to Backend Configurations
- Added the `coupling_dict` to the `backend_config.toml`
- Added `Dockerfile` and `.dockerignore`

### Fixed

- Fixed httpx version to 0.27.2 as 0.28.0 removes many deprecations that we were still dependent on in FastAPI testClient

### Changed

- Removed the `coupling_map` from the `backend_config.toml` as it is generated on-the-fly from the `coupling_dict`.

## [2024.09.1] - 2024-09-24

### Added

### Changed

### Fixed

- Fixed 'KeyError' when no units are not passed in the backend_config file
- Fixed "...bin/conda/activate: Not a directory" error when starting as systemd service
- Fixed silent error where calibrations are not sent to MSS on executor initialization
- Fixed "TypeError: Object of type datetime is not JSON serializable" when sending calibration data to MSS
- Fixed 'SyntaxWarning: 'is not' with a literal' when initializing backend

## [2024.09.0] - 2024-09-16

### Added

- The `QuantumExecutor` as abstract class to implement a backend
- `EXECUTOR_TYPE` keyword in the .env variables to select the backend
- Qiskit dynamics simulator backend with one qubit ("qiskit_pulse_1q")
- Added the initialization of the redis store with configuration picked from the `backend_config.toml` file
  when the execution worker starts
- Added an initial request to update the backend information in MSS
  when the execution worker starts

### Changed

- BREAKING CHANGE: `EXECUTOR_DATA_DIRNAME` definition in the .env variables instead of `general.data_dir` in `executor-config.yml`
- BREAKING CHANGE: Removed the whole `general` section in the `executor-config.yml`
- BREAKING CHANGE: Renamed `executor-config.yml` to `quantify-config.yml`
- Removed the old config files that were used for setting up automatic calibration
- Removed the script that loaded automatic calibration configurations at the start
- Moved the `backend_config.toml` file from `/configs` folder
- Moved the `properties` service to the `libs` folder
- Moved the `storage`, `date_time`, `representation` and `logging` utils to the `properties` lib
- Removed the `scripts` folder
- Removed the `archive` folder

### Fixed

- Fixed the reporting to MSS of errors in jobs during post-processing

## [2024.04.0] - 2024-05-28

This is part of the tergite release v2024.04 which merges the tergite stack into three clear parts: front, back, client/sdks

### Added

- Added storage_file lib (formerly tergite-quantify-connector-storagefile)
- Added `quantum_executor` service (formerly tergite-quantify-connector)
- Added the `executor-config.yml` and its python-based validators
- Added a simulator for one transmon qubit `qiskit_pulse_1q`

### Changed

- Changed the way discriminators are loaded to load from the database
- BREAKING_CHANGE: Removed hard-coded discriminators
- BREAKING_CHANGE: Removed official support for Python 3.8; Official support is now >=3.9
- BREAKING_CHANGE: Removed Labber support
- Replaced tergite-quantify-connector-storagefile package with an internal storage_file lib
- Moved unused files to `archive` folder
- BREAKING_CHANGE: Removed calibration and two state discrimination source code
- BREAKING_CHANGE: Replaced tergite-quantify-connector-storagefile package with an internal storage_file lib
- BREAKING_CHANGE: Merged tergite-quantify-connector into tergite-backend and renamed its service to `quantum_executor`
- BREAKING_CHANGE: Changed configuration of hardware to use `executor-config.yml` file with proper validations on loading
- BREAKING_CHANGE: Removed support for `Pulsar`, or any other instrument drivers other than `Cluster`  
  The old implementation wrongfully assumed that all these drivers have the same signature i.e. `driver(name: str, identifier: str | None)`  
  yet `SpiRack(name: str, address: str, baud_rate: int = 9600, timeout: float = 1, is_dummy: bool = False,)`,  
  `Pulsar(name: str, identifier: Optional[str] = None, port: Optional[int] = None, debug: Optional[int] = None, dummy_type: Optional[PulsarType] = None,)`  
  `Cluster(name: str, identifier: Optional[str] = None, port: Optional[int] = None, debug: Optional[int] = None, dummy_type: Optional[PulsarType] = None)` are all different.
- BREAKING_CHANGE: We got rid of quantify connector’s redundant reset() method.
- BREAKING_CHANGE: Changed backend name used when querying MSS for backend properties to be equal to `settings.DEFAULT_PREFIX`

### Fixed

- Fixed duplicate job uploads to respond with HTTP 409

## [2024.02.0] - 2024-03-19

This is part of the tergite release v2024.02 that introduces authentication, authorization and accounting

### Added

- Authenticated and authorized requests to/from clients including
  [MSS](https://github.com/tergite/tergite-mss) and the general internet.
- Tracking of execution time of jobs

### Changed

### Fixed

### Contributors

- Martin Ahindura

## [2023.12.0] - 2024-03-11

This is part of the tergite release v2023.12.0 that is the last to support [Labber](https://www.keysight.com/us/en/products/software/application-sw/labber-software.html).
Labber is being deprecated.

### Added

- Initial release of the tergite-backend server
- Support for [Labber](https://www.keysight.com/us/en/products/software/application-sw/labber-software.html)
- Support for [quantify-core](https://quantify-os.org/docs/quantify-core)

### Changed

### Fixed

### Contributors

- Miroslav Dobsicek
- Abdullah-al Amin
- Stefan Hill
- Axel Andersson
- David Wahlstedt
- Fabian Forslund
- Nicklas Botö

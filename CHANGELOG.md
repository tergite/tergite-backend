# Change Log

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project follows versions of format `{year}.{month}.{patch_number}`.

## [Unreleased]

### Fixed

- Fixed a circuit with a syntax error outside its `cal` blocks failing its job with nothing after "is not valid
  OpenQASM 3:"; the reason now says where the error is and what, e.g. "line 4, column 0: missing ';' at 'cx'"

## [2026.11.0-rc.10] - 2026-11-06

### Changed

- Changed the README back to a short quick start, and moved DESIGN.md's details into pages under `docs/` by reader:
  the dispatcher, MSS events, executors and deployment, with the conventions in CONTRIBUTING.md

### Fixed

- Fixed `uv sync` and `uv tool install` picking Python 3.14, on which the pinned pydantic-core does not build;
  the package now requires Python 3.13
- Fixed a `.env` copied from `.env.example` stopping the backend at start, as its MSS URLs were placeholders;
  they are now the defaults, MSS at `http://localhost:8002`

## [2026.11.0-rc.9] - 2026-11-05

### Added

- Added `docs/how_circuits_run.md`, on what the backend does to a circuit that can change a job's results: when its
  gates run, the 4 ns grid, resets and the phase turned on drives whose LO sits below the qubit

### Changed

- Changed a pulse-level block to hold up only the qubits its frames play on, where before it held up every qubit of the
  circuit; gates on other qubits now run beside it

### Fixed

- Fixed a circuit's qubits holding their state through another qubit's reset or measurement, as its gates ran one
  after the other; each now runs as late as the gates after it on its own qubits let it, as the SDK's pulse schedules did
- Fixed rz, a rotation's axis and a cal block's phase shifts turning the wrong way on a qubit whose drive LO sits below
  its frequency, as the qobj path's sign flip there went with it; they are negated there again, as the defcals' are

## [2026.11.0-rc.8] - 2026-11-04

### Added

- Added the `bcc` command to the package, so `uv tool install` installs bcc from its repository with its migrations,
  where before only the deb and macOS installers ran it outside a checkout
- Added the events the backend sends MSS over its websocket, `DeviceEvent` with its `Device`, `DeviceCalibration`,
  `Job` and `DeviceSwitchData`, and MSS's `EventResponse`, to the OpenAPI spec, so that MSS generates its models of them

### Changed

- Changed the linting from black, isort and pyflakes to ruff, which formats, sorts the imports and finds what
  pyflakes did; contributors run `ruff format` and `ruff check` where they ran the three
- Changed the default `.env`, and a relative `BCC_ENV_FILE`, to be read from the folder bcc starts in, where before
  they were read from beside `settings.py`
- Changed the calibration and the jobs the backend sends MSS to carry only the fields their models declare, where
  before they also passed on any other key a calibration seed or a job held
- Changed the debs and macOS packages to install uv's own build of Python with the dependencies `uv.lock` pins and the
  backend installed into it, where before they installed Nuitka's standalone build; a release builds them in minutes
  rather than in nearly two hours, and the deb is 305 MB, where Nuitka's was 426 MB

### Fixed

- Fixed MSS on PostgreSQL or SQLite never registering the backend's device, which repeated five of its config's
  values under names MSS does not declare, e.g. `num_qubits`; it, and `GET /static-properties`, now carry each once
- Fixed a device on clusters provisioned with `bcc-provision --device cluster` failing to start, as its example configs
  disagreed on its qubits; they now describe one device, q12, q13 and q14 in a row, which runs on dummy clusters as given
- Fixed `bcc start` from the debs and macOS packages, whose workers stopped as they started, unable to load openqasm3's
  and openpulse's parsers, which Nuitka left out, or to look for dask, which it excluded
- Fixed the packages' smoke test passing a heavy worker that stopped on any error but an import one, as rc.7's did on
  openqasm3's parser; it now fails on a worker that stops within its minute or logs a traceback
- Fixed gate-level jobs on clusters failing now and then with `Instrument with name q12_q13 does not exist` once the
  device's transmons or coupler edges had been made anew, as after a recalibration, or a coupler had left its calibration

## [2026.11.0-rc.7] - 2026-11-04

### Added

- Added the job file, `JobFile` with its `CircuitJobParams` and `QasmCircuit`, to the OpenAPI spec, as the JSON the
  upload to `POST /jobs` carries, so a client generates it rather than writing it by hand

## [2026.11.0-rc.6] - 2026-11-04

### Added

- Added `BCC_SHOULD_RECALIBRATE_AT_START`, off by default: a device now starts with its calibration seed and first
  recalibrates an interval later, where before every start ran tergite-tuner for minutes before any job; set it to
  recalibrate at start as before

## [2026.11.0-rc.5] - 2026-11-04

### Fixed

- Fixed devices on dummy clusters, the simulated ones among them, failing recalibrations or jobs with "Assembly failed"
  when one `bcc start` served several of them from one folder: their heavy workers each assembled in `./tmp.q1asm`, and
  now each runs in its device's own folder

## [2026.11.0-rc.4] - 2026-11-04

### Added

- Added simulated devices as simulated chips: a `[simulator.chip]` section of a chip's true parameters, couplers with
  their parametric CZ among them, puts it behind a device's dummy clusters, which play each schedule compiled for them on
  it, so its jobs run through quantify's whole path and tergite-tuner calibrates it as it would real hardware
- Added how a gate-level job ran to the result it sends MSS: its shots, `meas_level`, `meas_return` and each circuit's
  name and number of classical bits, so the SDK reads a job's result back without its payload
- Added gate-level jobs: `POST /jobs` takes OpenQASM 3 circuits, with `shots`, `meas_level` and `meas_return`,
  in place of a qobj, and the quantify executor and the simulators compile them with the device's own
  calibration; their memory has a qobj's shapes, each measurement in its circuit's classical bit
- Added pulse-level jobs on the quantify executor: a gate-level job's OpenQASM 3 may hold OpenPulse `cal`
  blocks and `defcal`s, which play OpenPulse's waveforms and capture IQ points on the frames each device
  publishes (`q12_drive`, `q12_readout`, `q12_q13_coupler`); the simulators refuse them
- Added limits to pulse-level jobs: a device takes them only from projects MSS says may send them, as
  `is_pulse_allowed` on asking for a job's token, and only within the `pulse_limits` of its backend config,
  checked at upload and before they run; a device without `pulse_limits` refuses them all
- Added each device's frames and its gates' `defcal`s to the calibration it sends MSS, where it takes pulse-level
  jobs: x, sx, rz and cz as the quantify executor plays them, sample for sample, which a job may copy or override
- Added `dev/check_gate_level.py`, which checks the configs of each device env file for gate-level jobs,
  without the instruments: missing calibration or LDA values, couplers whose CZ plays no pulse, and ports the
  compiler refuses or whose LO puts an NCO beyond ±500 MHz

### Changed

- Changed recalibration to run tergite-tuner on the SPI rack the quantify metadata lists, a real one on its serial port,
  where before it always ran a dummy rack and so never set a coupler's current
- Changed which qubit of a coupler gets each of its CZ's phase corrections to the roles tergite-tuner's device config
  gives it, `control_qubit` and `target_qubit`, kept in the calibration; before, the even-numbered qubit was taken as the
  control, so a device configured otherwise had its corrections swapped
- Changed the `basis_gates` each device publishes to MSS to the gates gate-level jobs are transpiled to (rz, sx,
  x, rx, measure, reset and delay, and cz where it has couplers), from the names of its config's gate settings
- Changed `POST /jobs` to take OpenQASM 3 circuits only, deleting the qobj path: an older SDK's qobj job is now
  refused with 400, "the backend takes OpenQASM 3 circuits only, not a qobj", so send circuits, pulses as OpenPulse
- Changed the backend to install as one package with no extras, one `bcc` binary replacing `bcc` and `bcc.simulator`,
  and deleted the qiskit-dynamics simulators: `BCC_EXECUTOR_TYPE=qiskit_pulse_1q`/`qiskit_pulse_2q` is now refused, a
  simulated device being executor type `quantify` with a `[simulator]` section in its backend config
- Changed the simulated devices' names, and their example seeds', to `sim_1q` and `sim_2q`, as `bcc-provision` calls
  them; a device provisioned before keeps the name its backend config gives it

### Fixed

- Fixed recalibrating a device with couplers, which failed at Ramsey 12: the node, run one qubit at a time, compiled a
  device holding the edge of a qubit it lacked; tergite-tuner 2026.10.0rc6, now required, leaves those couplers out
- Fixed the first wait before reconnecting to MSS being a second whatever `mss_max_reconnection_delay` said, which
  held every first retry to a second however low it was set
- Fixed a quantify job with `meas_return` avg failing in postprocessing, as its averaged results, which have no
  repetitions, could not be read back
- Fixed every quantify job after a recalibration failing with "'InstrumentCoordinator' object and its
  delegates have no attribute 'components'" until the heavy worker restarted; the executor reconnects its clusters
- Fixed a simulator job that measured some qubits but not those before them failing with "error during post
  processing"; qiskit-dynamics returns a column for every memory slot up to the last, and the unmeasured ones are skipped
- Fixed a job cancelled with its job token, as the SDK cancels, staying pending in MSS for good; the
  cancellation is now sent to MSS, as every other change of a job's status is

## [2026.11.0-rc.3] - 2026-10-31

### Changed

- Changed a tag's GitHub release notes, which only said "See CHANGELOG.md.", to its CHANGELOG.md section; a tag
  without one now fails the release workflow before it builds anything

## [2026.11.0-rc.2] - 2026-10-31

### Changed

- Gave every public function and class a Google-style docstring, saying what it does for the caller, its arguments,
  what it returns and what it raises

### Fixed

- Fixed the GitHub release of a tag getting no installers: it is now published with them, as an immutable release
  took none once published
- Fixed a release candidate's GitHub release being published as the latest release; it is now a pre-release

## [2026.11.0-rc.1] - 2026-10-31

### Added

- Added a systemd watchdog to `bcc@.service`, `WatchdogSec=60s`: `bcc start`, and a `bcc worker` its unit runs alone,
  tell systemd they are alive while on time, so a hung one is restarted
- Added `GET /health`, unauthenticated, which `bcc start` asks every 5 s, killing an API that misses 3 in a row, as a
  hung event loop left it running but deaf
- Added `BCC_MAX_WORKER_START_TIME`, 10 minutes by default and 0 for none: `bcc start` kills a worker that has not
  finished starting by then, e.g. stuck connecting to the clusters, which went unnoticed
- Added `<seed>.tuner.json` beside the calibration seed file, tergite-tuner's results of the last recalibration, which
  the next one starts from, as tergite-tuner keeps nothing between its sessions
- Added `BCC_MAX_EXECUTION_QUEUE_TIME`, a day by default and 0 for none: a preprocessed job waiting longer to execute,
  e.g. for room between bookings, is failed and its user told, where it waited indefinitely
- Added `BCC_MAX_WORKER_WAIT_TIME`, the longest a worker with nothing to do waits before looking again, which bounds
  how late a lost wake-up, a due recalibration or a dead worker's job is noticed; default 5 seconds, as before
- Added GitLab jobs that, on a `v*` tag, build the debs and macOS packages, upload them to the package registry and
  link them from the GitLab release; the macOS ones run where `PUBLISH_MACOS_ARM64` is `true`, on a runner tagged `macos`
- Added amd64 debs, for Debian 13 and Ubuntu 24.04 onwards, and macOS arm64 packages, `tergite-backend` and `tergite-backend-simulator`, running each
  backend instance, named for its `DEFAULT_PREFIX`, as a `bcc@<name>` service set up by `bcc-provision`
- Added `bcc start`, the default command, to the Nuitka binary, which runs the API, one worker for preprocessing, executions and
  recalibrations, and `--light-workers` (default 1) for postprocessing as one process tree
- Added `--env-file`, given once per device, to `bcc start`, `serve` and `worker`, which serves several devices from one
  process, each device's API under `/<DEFAULT_PREFIX>/`, with a heavy worker per device and light workers shared by all
- Added `--workers` to `bcc start` and `serve`, the number of API processes, defaulting as uvicorn does to
  `$WEB_CONCURRENCY` or 1; `BCC_ENV_FILE` may name several env files, separated as in PATH, as `--env-file` does
- Added `BCC_<NAME>` env variables, e.g. `BCC_MAX_IDLE_TIME`, which set every device's `<NAME>`, and
  `BCC_<PREFIX>_<NAME>`, e.g. `BCC_LOKE_MAX_IDLE_TIME`, one device's over them

### Changed

- Changed the expired MSS nonces to be deleted by the workers as they look for work, instead of by every request
  from MSS
- Changed the quantify extra's tergite-tuner to 2026.10.0rc3 or a later 2026.10 release, which keeps its sessions
  in memory instead of Redis
- Changed `GET /jobs` to list 100 jobs unless `limit` asks for more, up to 1000, where it listed all of them; page
  through with `skip`
- Changed the job supervisor log to plain lines, as Python's logging writes them, without the ANSI colour codes
- Changed a recalibration's calibration to hold every stored value, not only those the run set, so it no longer
  fails on values the run leaves alone, such as `acq_delay`, with tergite-tuner's exact `read_result` matching
- Changed the jobs to be kept in the bookings database, `BCC_BOOKING_DB_URL`, instead of Redis; jobs stored in
  Redis before upgrading are no longer listed, so let the queues empty first
- Changed whether a device is switched off to be kept in the bookings database instead of Redis; a device switched
  off before upgrading is on after it, so switch it off again
- Changed the device parameters and MSS's request nonces to be kept in the bookings database instead of Redis
- Changed the bookings database to be created and upgraded with Alembic as the API and the workers start, instead of by SQLModel;
  an existing one is kept as it is, but for its unused `user.password` column, which is dropped
- Changed the 422 for a booking that starts in the past or lasts outside the device's limits to give its message
  as `detail`, as the other errors do, instead of pydantic's list of errors
- Changed the workers to take their jobs and recalibrations from the bookings database instead of RQ's queues, and
  `bcc rq --kind` to `bcc worker --kind`; work queued in RQ before upgrading is dropped, so let the queues empty first.
  An idle worker waits on a UDP port of 127.0.0.1, which the API wakes it on
- Changed a booker's job submitted before their booking to run in it, ahead of other users' jobs, instead of waiting
  behind them
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

- Fixed a packaged instance's quantify recalibration failing to rewrite its calibration seed, which provisioning put
  in the service's read-only `/etc/bcc/<name>/`; it now goes in `/var/lib/bcc/<name>/`, so move an existing one there
  and point `BCC_CALIBRATION_SEED` at it
- Fixed the calibration a heavy worker publishes at start giving its couplers no control or target qubit; they are
  named from the device config's `coupling_dict`, and a coupler missing from it is refused at start
- Fixed a recalibration's results reaching only MSS and the seed file: jobs ran on the old qubit frequencies and were
  discriminated with the old calibration, which `/dynamic-properties` still served, until the heavy worker restarted
- Fixed a light worker serving several devices postprocessing the first device's jobs before any other's; it now
  takes the devices in turn
- Fixed a recalibration whose worker died or was killed showing as under way in `/recalibration/info` from then on;
  a heavy worker starting marks it failed
- Fixed a worker stuck in a call its time limit cannot interrupt, e.g. an instrument driver's, stalling its device until
  someone restarted the backend; `bcc start` now kills a worker past its deadline, which restarts the backend
- Fixed an unreachable MSS stopping the workers, and failing a job that had succeeded when its results could not be
  sent; the workers keep the events MSS cannot take, in order, and send them once it can
- Fixed a reply from MSS being awaited past `BCC_MSS_RESPONSE_TIMEOUT` while other messages kept arriving, each of
  which restarted the wait
- Fixed an event to MSS whose connection dropped before MSS replied timing out, which failed a job that had succeeded;
  the event is sent again on the new connection
- Fixed a restart of the API alone overwriting, in Redis and MSS, the calibration the heavy worker published, e.g. the
  simulators' trained discriminators, so that jobs were discriminated wrongly until the worker restarted; the heavy
  worker alone publishes the device's properties and calibration now
- Fixed `BCC_ARE_CLUSTERS_RESETTABLE` being ignored: the quantify executor never reset its clusters, whatever it was set to
- Fixed bookings and recalibrations never taking effect: no release's workers ran RQ's scheduler, so a booking's
  start and end, and recalibration's start and repeats, never ran
- Fixed replayed MSS requests being accepted while the first was still running, or after it crashed: the nonce is
  now claimed before the route runs, instead of recorded after it
- Fixed cancelling a job leaving its rq job queued: cancellation looked for an rq job id that was never used, so the
  rq job still ran and took whatever job was next in its queue
- Fixed queue durations going negative: cancelling a job lowered the total duration of every queue it was not in,
  so the scheduler thought more time was free before a booking than there was
- Fixed the running job's time being left out of an execution queue's duration, so a job could be queued as fitting
  before a booking, or in what was left of one, when it did not
- Fixed switching one device off taking every device served from the same Redis offline
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

- Removed Redis, which nothing needs since tergite-tuner 2026.10.0rc2: `BCC_RQ_REDIS_URL` and the `BCC_REDIS_*`
  settings are ignored, and the deb and `bcc-provision` no longer ask for one
- Removed RQ and `BCC_MAX_GENERAL_QUEUE_TIME`, and the `storage_id` of jobs and the RQ job ids of bookings, which only
  RQ's queues read; macOS no longer needs `OBJC_DISABLE_INITIALIZE_FORK_SAFETY`, as nothing forks
- Removed `BCC_JOBS_STORE_CLEAN_INTERVAL`: a job older than `BCC_JOBS_STORE_TTL` is deleted as another is stored
- Removed `BCC_REQUEST_LOG_TTL` and `BCC_REQUEST_LOG_CLEAN_INTERVAL`, with the two weeks of requests the BCC kept in
  Redis and read nowhere; MSS's nonces are still kept for `MSS_NONCE_TTL`
- Removed `BCC_IS_ASYNC`, which ran each job inside the API request that queued it; the jobs run on the workers only
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

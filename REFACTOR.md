# Refactor plan

Nine steps, in order. Each numbered item is one commit, verified before the next. Items marked **Decide** need the
maintainers' answer before they start.

## Conventions

These are the rules MSS follows (tergite-frontend `DESIGN.md` §14, "Code structure" and "Python style"), applied to
this repo. Every step applies them to the code it touches, and new code follows them too. Step 8.1 moves them into
this repo's own `DESIGN.md`.

1. **A function is a composition of functions.** A public function's body is a short sequence of calls, each to a
   function that does one thing and is named for it. A reader gets the whole flow from that body and drills into a
   step only when they need its detail.
2. **Two exceptions.** Do not extract a step that would be a one-line function. Do not split a performance-critical
   path, such as the executors' inner loops, where the extra calls cost something measurable.
3. **Callees sit below their callers.** A file runs: constants, public functions in the order a caller uses them,
   then private helpers in the order they are first called.
4. **A comment that labels a phase is a missing function.** `# Construct the pulse schedule` becomes the name of the
   function that does it.
5. **Data is a struct; state gets a class.** Data is a Pydantic model, a frozen `@dataclass`, an `Enum` or an
   `Exception`. Its only methods are derived views of its own fields and conversions named `from_*` or `to_*`, even
   when a conversion reads a file or a store to fill itself in. A component that owns state across calls (a live
   websocket, an instrument coordinator, a trained discriminator) is a class with behaviour.
6. **Stateless behaviour is a module-level function** taking explicit arguments. No objects built only to carry
   other objects, no `Protocol` or ABC with one implementation, no inheritance used to share behaviour.
7. **Dependencies arrive as arguments.** No function or class reads a global, in its body, as a default argument or
   as `param or settings.X`. Only the composition roots read `settings`: the API's lifespan and dependencies, and the
   RQ worker's start-up. They turn config into instances before handing them on, so code that needs Redis, the
   database or MSS is given the connection, store or client, never a URL. The one exception is a module caching its
   own private global, such as a loaded key.
8. **One home per kind of thing.** Routes live in `app/api/routers/`. `HTTPException` is raised only in `app/api/`.
   Domain errors subclass `app/utils/exc.py` and are mapped to status codes once. A service's `__init__.py` only
   re-exports, with `__all__` as strings. Imports run one way, `api → services → libs`: `libs` imports nothing from
   `api`, `services` or `settings`. Development and test tooling lives in `dev/`.
9. **A reputable library beats our own code.** Write our own only where no well-maintained library does the job, or
   where getting the behaviour we need would mean fighting it. The commit that adds our own code says which.
10. **A name says what the thing is.** Variables are nouns naming their content (`job_filter`, never `data`, `out`
    or `kind` for something specific), functions are verbs, booleans start with `is_`/`should_`. No `build_*`,
    `generate_*`, `construct_*` or `make_*` for a thing that is constructed: that is a class's constructor, and
    anything else is got, so the function is a `get_*`.
11. **Obvious beats clever.** An `if`/`else` returning each whole literal, one explicit `if` per optional field; not
    loops over tuples of fields, comprehensions that build operators, merged dicts (`{**a, **b}`) or one helper
    returning two things to be pulled apart.
12. **A value that is only defined stays where it is used.** A function that does no more than return a literal makes
    the reader jump to it and back for nothing.
13. **Every request, job and store is scoped to its device.** One process serves several devices, so whatever a device
    owns (queues, jobs, bookings, the suspension flag, the executor lock) is keyed or held per device, never shared by
    accident of a common Redis or working directory.
14. **Unchanged:** the Apache 2.0 licence header on every file, constants at the top, module-private helpers at the
    bottom, no banner comments, comments only for what the code cannot say, Google-style `Args:`/`Returns:`/`Raises:`
    only where a function takes several arguments or raises domain errors.

Rule 1 applied to `submit_job_file` (`app/services/scheduler/service.py:181`), whose body today is phase comments
over 44 lines (signature illustrative; `DeviceResources` is step 4.1's):

```python
def submit_job_file(device: DeviceResources, upload_file: UploadFile, claims: MSSTokenClaims) -> Job:
    job_file = _save_job_file(device.job_upload_pool, upload_file, claims.job_id)
    job = _insert_job(device.jobs_store, claims, job_file)
    _enqueue_preprocessing(device.queues, job)
    return job
```

**Every commit is verified the same way.** `black` and `isort` with no flags, `pyflakes app main.py settings.py`, then
the tests the change can affect, run serially: `pytest <files> > "$LOG" 2>&1; echo "exit=$?" >> "$LOG"`. The two
executor extras conflict, so a change that reaches executor code runs in both environments: the project's `.venv`
(quantify) and one synced with `UV_PROJECT_ENVIRONMENT=.venv-qiskit uv sync --dev --extra qiskit`. The style rules a
parser can see are tests too, from step 1.9: a commit that fixes a violation deletes its line from the baseline, and
none adds one. `DESIGN.md`, once step 8.1 adds it, changes in the same commit as any file it names.

**Every step ends with a sign-off.** The full suite runs in both environments; tergite-frontend's
`dev/e2e_native_test.sh` runs against this branch (`BACKEND_BRANCH`); step 1.10's performance check runs once and is
compared with the baselines, never re-recorded for an older commit. A user or an agent then tries the step's changes
on a running BCC (a simulator device with MSS) and gives the go-ahead before the next step starts.

## Step 0: fix the bugs

The only behaviour changes in the plan. Each commit adds a test that fails before the fix and gets a `Fixed`
changelog entry.

1. **Nonce replay.** `get_verified_mss_user_id` only asks whether the nonce exists
   (`app/api/dependencies.py:264-266`); the `log_requests` middleware stores it after the route has run
   (`app/api/__init__.py:179-181`). The same signed headers are accepted again while the first request is in flight,
   and, if the route raised, for as long as their timestamp is within `MSS_NONCE_TTL`. Claim the nonce atomically before the route, with Redis `SET NX EX MSS_NONCE_TTL`.
2. **Cancelling never reaches RQ.** Jobs are enqueued with the id `{job_id}_{STAGE}`
   (`app/services/scheduler/utils.py:189`, used at `service.py:262` and `tasks.py:273, 525, 600, 637`), but
   `RunnerQueue.cancel_job` looks for `{queue}_{job_id}` (`app/libs/queues/types.py:369, 397`), and the
   `NoSuchJobError` is suppressed. Use one id function for both.
3. **Queue durations go negative.** `StaticQueue.pop_job` subtracts the job's duration even when `lrem` removed
   nothing (`types.py:154-164`), and `_cancel_job_in_queues` (`service.py:660-686`) tries all five queues, so every
   cancel lowers four queues' `total_duration` and inflates the time thought free before a booking. Subtract only
   when `lrem` returned 1, in one Lua script. In the same commit: `clear()` leaves `__args_kwargs_{name}` behind
   (`types.py:135-138`).
4. **The running job's time is not counted.** The snapshot taken at dequeue has no execution start, so its
   `current_eta` is `None` (`types.py:313-319` with `:124`), after `next()` has already subtracted its duration.
5. **Switching one device off stops every device.** `switch_off`, `switch_on` and `is_offline` use RQ's global
   `rq:suspended` key (`service.py:597, 623, 639`). Keep a suspension flag per device (rule 13).
6. **Devices see each other's jobs.** The jobs hash is named after the schema only (`app/utils/redis_store.py:182`)
   and the store is keyed by Redis URL (`app/services/scheduler/store.py:44`); `main.py start` runs every device on
   one Redis (`main.py:264-269`). So `/A/jobs` lists device B's jobs, and cancelling B's job through A marks it
   cancelled while it runs. Key the jobs by device. In the same commit, the executor lock
   (`app/libs/quantum_executor/utils/connections.py:19`, a `FileLock(".quantum-executor.lock")` in the working
   directory) becomes one per device, at a path the root passes in.
7. **Failed jobs stay `EXECUTING`, and MSS never hears of a failure.** `postprocess` calls `move_file`,
   `read_job_from_hdf5` and `update_job_stage` outside its `try` (`tasks.py:313-318`), so their errors bypass the
   failure callback (`tasks.py:390`); its `NotImplementedError` message reads `job.meas_level` from the stored job
   (`tasks.py:341`). Preprocessing and execution failures, and "too long for the booking"
   (`tasks.py:136-144, 283-289, 583-589`), are written to Redis only, because those queues have no failure callback
   (`app/services/scheduler/queues.py:227, 264, 302`). Every stage marks the job `FAILED` and tells MSS.
8. **One failed recalibration ends recalibration for good.** `recalibrate` schedules the next run only on success
   (`tasks.py:418-456`). In the same commit: `/recalibration/cancel` skips the running job, which re-enqueues itself
   when it finishes (`service.py:556-557`), so the chain cannot be stopped.
9. **Finished jobs can be cancelled.** `cancel_job` has no terminal-status check (`service.py:309-320`), so a
   `SUCCESSFUL` or `FAILED` job becomes `CANCELLED`. In the same commit, the `job` it returns can be unbound when a
   suppressed RQ error is raised (`service.py:349-367`).
10. **MSS admins are refused until they have fetched a token.** The user must exist in the bookings database before
    `is_mss_admin` is checked (`service.py:155-157, 297-300, 393-396`), so an admin gets 401 or 404.
11. **Jobs overrun a booking.** `usable_time` uses the booking's whole `total_duration`, not the time left in it
    (`tasks.py:576`). And a job whose booking ended or was deleted before preprocessing finished goes to a waitlist
    that only a booking's end drains (`service.py:252`, `tasks.py:567-605`), so with no later booking it waits for
    ever. **Decided:** such a job falls back to the normal queue.
12. **Deleting a user leaves work behind.** Only `PENDING` jobs are cancelled, the reason is written to
    `failure_reason`, and an active booking's end event is cancelled so `post_booking_cleanup` never runs
    (`service.py:460-486`).
13. **Concurrent writes.** The booking overlap check and insert are separate (`app/services/booking/service.py:64-71`),
    so two API workers can book the same slot; and the user get-then-create races, as does a duplicate `POST /users`
    (`app/api/__init__.py:314-317, 415-418`), raising an unmapped `IntegrityError`, a 500. Check and insert in one
    `BEGIN IMMEDIATE` transaction, and map `IntegrityError` to 409.
14. **`/switch/*` blocks the event loop.** The `async` routes (`api/__init__.py:761, 779`) call the blocking
    websocket `send_event`, whose backoff is `time.sleep` (`app/services/external/mss/service.py:170, 219`), freezing
    every request on that API worker for up to 120 s. Make them plain `def` routes.
15. **Small ones**, one commit each:
    - `get_next_job_on_queue` sorts on `started_at`, which is `None` for queued and scheduled jobs: a `TypeError`
      when both exist (`app/utils/rq.py:110`)
    - a naive `start_timestamp` from the query is read by RQ as server-local time while the rest of the code reads
      naive as UTC (`service.py:520-526`); convert with `to_utc()`
    - `online_date: str = utc_now_str()` is evaluated once, at import (`app/libs/device_parameters/dtos.py:786`)
    - `train_discriminator`'s loop overwrites the same keys, keeping only the last row
      (`app/libs/quantum_executor/qiskit/backends/two_qubit.py:342-346`, `one_qubit.py:276-280`), and
      `QuantifyExecutor.recalibrate` keeps only the last cluster's results
      (`app/libs/quantum_executor/quantify/executor.py:192`); confirm the intent of each before fixing.
      **Found:** a two-class LDA has one row of coefficients, so the discriminator loop runs once and loses
      nothing; 6.5's `_fit_lda` removes it. tergite_tuner calibrates the whole device through one cluster, so
      each pass of `recalibrate`'s loop recalibrated everything: it now refuses more than one cluster instead.
16. **Status codes.** `IsOfflineError` is 404 (`api/__init__.py:119`), a missing job 403
    (`dependencies.py:330-332`), and MSS rejecting or timing out a request a 500 (`api/__init__.py:112-116`).
    **Decided:** 503, 404, and 502/504; MSS's `BccClient` passes the BCC's code on to its caller and checks none.
17. **What the signature covers.** The signed message is `user_id-nonce-timestamp` (`dependencies.py:197, 217, 251`):
    `x-mss-is-admin`, the method and the path are not signed, so on a plain-HTTP link anyone in between can turn on
    admin or move a signature to another route. **Decided:** sign `is_admin|method|path` too, together with MSS's
    `_create_headers` in tergite-frontend `services/external/bcc/service.py`, released together.

## Step 1: pin behaviour with tests

Tests and tooling only. After this step, a refactor commit that changes behaviour fails a test.

1. **The HTTP contract.** Every row has a test; add the missing ones. Every later step keeps every row.

| Surface | Behaviour |
| --- | --- |
| MSS-signed requests | headers `x-mss-user-id`, `x-mss-request-id` (nonce), `x-mss-timestamp`, `x-mss-signature`, `x-mss-is-admin`; a bad signature, a reused nonce, a timestamp older than `MSS_NONCE_TTL` or in the future → 401 |
| `POST /token` | body `user_id` other than the header's → 403; an unknown user is created; the `access_token` is the job JWT encrypted with MSS's public key (today's test reads the raw JWT from a `jwt.encode` spy, `app/tests/test_api.py:3355`) |
| Job JWT | expires after `JWT_TTL`; `/jobs/{id}/cancel` accepts MSS headers or the bearer token, and a token for another job → 403 |
| Device routing | `/<prefix>/…` reaches that device for every route group, not only `/users` (`app/tests/test_devices.py:74`) |
| Offline device | the routes that change or list its work, through `get_queue_context_if_online`, answer 503 as step 0.16 decided; the read routes, `/token`, `POST /users` and `/switch/on` answer as usual. Refusing every route would change behaviour, so step 1 pins this |
| `X-Request-ID` | echoed on every response, and the request logged once |
| Error mapping | each domain error's status code, and 500 for an unexpected exception |
| `GET /bookings/config` | its field set |

2. **MSS events.** `MockWebsocket` records its outbox but nothing reads it (`app/tests/utils/mss.py`); assert the
   payloads of `initialized`, `recalibrated`, `job_updated` and `switched_*`, verify their signatures instead of only
   their presence, and make `recv` pop in order (it pops last-in-first-out today). Add MSS's error reply, a response
   timeout and a reconnect during `recv`.
3. **Queues and scheduling.** A job killed by an RQ timeout ends `FAILED`; execution and postprocessing failures end
   `FAILED` and reach MSS (after step 0.7); a failing recalibration schedules the next (after 0.8); adjacent bookings
   (end equal to the next start); `MAX_SLOTS_PER_DAY` across midnight UTC and per user; the `BCC_CURRENT_DATE`
   override.
   **Found:** a job token's `exp` is `BCC_CURRENT_DATE` plus `JWT_TTL` (`booking/service.py:360`), while PyJWT
   checks it against the real clock, so on a device whose `BCC_CURRENT_DATE` is past every job token is born
   expired. The test pins the override for bookings only. **Fixed** after step 1: a token's expiry is from the real now.
   **Found too:** `QuantifyExecutor.__init__`, building for hardware it has not cached, calls `Cluster.close_all()`
   before closing the cached `ClusterComponent`s, which then fail on their closed clusters
   (`quantify/executor.py:155-156`); a process building two quantify configs crashes. **Fixed** after step 1.
4. **The suite runs in parallel.** Workers of `pytest -n auto --dist=loadscope` share one Redis database, which every
   teardown empties with `flushall` (`app/tests/conftest.py:170, 351, 397`), one SQLite file (`test_booking.db`, from
   `app/tests/utils/env.py:21`), `/tmp/jobs`, and the working directory, where the qblox dummy writes `tmp.q1asm` and
   `tmp.hex`. `setup_test_env()` derives a Redis database from `PYTEST_XDIST_WORKER`; teardowns use `flushdb`; each
   worker gets its own SQLite file and storage roots under `tmp_path_factory`; the executor lock's path is passed in
   (step 0.6); the suite runs from a temporary working directory. `env.py` also defaults to port 6379 while CI and
   `CONTRIBUTING.md` use 6378: one default.
   **Done** with `tempfile.mkdtemp` instead of `tmp_path_factory`: the settings read the folders when `conftest.py`
   is imported, before any fixture runs. The default port is 6378.
5. **The suite runs fast.** Build the app once per session, as MSS's suite does, instead of reloading `settings` and
   `app` and running the lifespan for each of about 120 tests; preload the executor once per session, not in every
   `PreloadedSimpleWorker.__init__`; replace `test_store.py`'s real sleeps (about 21 s) with `time-machine`, which then
   replaces `freezegun` and `pytest-freezegun` too.
   **Done otherwise, by measurement.** Reloading the app and running its lifespan cost about 0.04 s a test, and
   the worker fixtures 4.6 s in all, while creating a user cost 0.25 s, bcrypt hashing at its default cost: the
   tests now hash at its lowest, which took the serial suite from 142 s to 80 s. One app for the session would
   need one app serving the three devices and a session-long lifespan whose per-test effects (start-up events,
   the recalibration it enqueues) many tests rely on; step 4 rebuilds the app's construction, so it is left to
   it. `test_store.py`'s sleeps stay: Redis expires the fields itself, which no Python clock moves, and step 5.3
   replaces the store. `freezegun` and `pytest-freezegun` go: `test_store.py`'s `freezer` is a time-machine fixture.
6. **Test helpers by job**, in `app/tests/utils/`: from `conftest.py` (568 lines), the parameter lists to `params.py`,
   the LDA mocks to `analysis.py`, `redis_client` and the three `rq_worker*` fixtures, which differ only in a prefix,
   to `redis.py` and `rq.py`, the three REST-client fixtures to `clients.py` (made consistent: today only the qiskit
   ones flush Redis and only some patch `connect`), `fake_clock` to `datetime.py`, the MSS fixtures to `mss.py`, the
   folder fixtures to `io.py`, the connection spies to `connections.py`; and `test_api.py`'s 720 lines of private
   helpers (`app/tests/test_api.py:2849-3573`) to the module of what they help with, keeping one of the two
   `_submit_multiple_jobs*`. `conftest.py` keeps the environment set-up and registers the rest with `pytest_plugins`.
   Delete `MOCK_NOW`, `CLIENTS`, `RECALIBRATION_MOCKS` (a copy of `utils/tuner.py:23`), `utils/mocks.py`,
   `utils/redis.py`'s two functions, `records.with_incremental_timestamps` and `mss.mock_mss_websocket_handler`.
7. **Split `test_api.py`** (3,573 lines, 127 tests) by route group, into `app/tests/api/test_{devices,users,bookings,
   jobs,recalibration,switch,logfiles}.py`, so that step 3.1's routers map onto them one to one.
   **Done** with an eighth, `test_app.py`, for what every route does: MSS's signed headers and nonces, request ids,
   error mapping, an offline device and start-up locking. The parameter lists several modules share moved to
   `utils/params.py`.
8. **CI.** The GitLab cache key names `$EXTRA_NAME`, which is never set (`.gitlab-ci.yml:50`): `$DEPS_GROUP`. The
   qiskit job skips 12 booking, queue and recalibration tests on `skipif(not HAS_QUANTIFY)`; those that need quantify
   only for a mock executor run on the simulator's instead, so both jobs cover the scheduler.
   **Done** for nine: six job tests, the recalibration info and the start-up lock, as the simulators' tests use a
   stub executor now, and the qiskit job runs 441 tests, not 425. Four stay quantify-only: three check the seed
   quantify's recalibration writes, and one counts real seconds against a booking start that the simulators'
   slower compilation eats into. The booking tests count real seconds, so they can fail under a heavy `-n auto`.
9. **Style rules as tests.** Port tergite-frontend's `tests/test_styleguide.py` to `app/tests/test_styleguide.py`
   for rules 3, 8, 10 and 14 (verbs, booleans, constants and private helpers in place, banners, licence headers,
   `__all__` strings, the import direction, `HTTPException` only in `app/api`, full type hints), with
   `app/tests/fixtures/styleguide_baseline.txt` listing today's violations.
10. **Performance baselines**, recorded before any refactor commit and rerun at every sign-off. `dev/perf/` holds a
    Locust scenario driving `POST /jobs` (upload, validation, enqueue), `GET /jobs` and `GET /bookings` against a
    stored backlog, and the MSS-signed request path, against a BCC from the working tree with a stub executor, so the
    numbers measure the scheduler and not the physics. It also records each stage's queue wait and run time from
    `Job.timestamps`, samples the CPU and memory of the API and of the heavy and light workers with psutil, and
    counts Redis memory and keys. `record.sh` and `compare.py` follow MSS's; baselines live in
    `dev/perf/baselines/`. A step whose p95 latency, stage overhead or peak memory is more than 10% worse is not
    signed off until the cause is found.
    **Done first in step 1**, before 1.2, so that the baselines are of the code as step 0 left it. The BCC runs one
    `qiskit_pulse_1q` device against a stub MSS (`dev/perf/mss_stub.py`). On macOS the recorder sets
    `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`, as `start_bcc.sh` and the macOS package's launchd plist do: without
    it the Objective-C runtime kills every forked work horse, so `main.py start` or `rq` run directly on macOS
    finishes no job's postprocessing.
    *Re-recorded at step 4's sign-off*, of step 4's code: RQ's scheduler runs in a spawned process that re-imports
    the main module, and `dev/perf/stub_worker.py` imported qiskit at the top, so the heavy worker's peak counted
    about 300 MiB that `main.py` does not load. The stub executor now lives in `dev/perf/stub_executor.py`, imported
    under the `__main__` guard; later steps compare with step 4.
11. **Tooling out of the app.** `start_bcc.sh` moves to `dev/`; nothing writes into the working directory or under
    `app/tests/` (`tmp/`, `tmp.*`, `test_booking.db`); `settings.FIXTURES_DIR` (`settings.py:44`) stops pointing
    into the tests.

## Step 2: delete dead code and dependencies

Each item greps for its names once more before deleting.

1. **Dead code**, one package per commit:
   - `app/api`: `LoggingRqWorker` (`worker.py:49`), `oauth2_scheme` (`dependencies.py:62`), the `print` at
     `dependencies.py:179`, and `add_request_id_header` (`__init__.py:139-158`), which `log_requests` duplicates
   - `app/utils`: `NotAllowedError`, never raised, and `NotInstalledError` (`exc.py:60, 64`)
   - `app/services`: `_get_current_recalibration_job_id` (`scheduler/service.py:707`), `_get_idle_timer_id`
     (`tasks.py:473`), the unreachable branch at `tasks.py:603-607` and its docstring's "from waitlist" case,
     `_get_redis_url` (`scheduler/utils.py:453`), `MssClient.__enter__`/`__exit__` and its unused `__current_delay`
     (`mss/service.py:110, 247-253`), `MSSTokenClaims.__primary_key_fields__` (`booking/models.py:37`), the `print`s
     at `tasks.py:285, 365, 367`
   - `app/libs/device_parameters`: `get_backend_config` (`__init__.py:37`)
   - `app/libs/quantum_executor`: `_schedule_to_bytes`/`_bytes_to_schedule` (`qiskit/experiment.py:137, 151`);
     `unique_name`, `final_timestamp` (`quantify/instruction.py:131, 139`); `worker_logger` and the never-read coupler
     maps (`quantify/executor.py:57, 134-141`); the `print`s at `quantify/executor.py:285, 295, 297`; `SpiRackConfig`
     (`quantify/utils/config.py:110`); `_CalibrationResult`, `_QUBITS_NODE_RUN_MAP` (`quantify/utils/calibration.py:22,
     34`); the `mode="dict"` branch (`base/quantum_job/__init__.py:245`) and the dead lines in `QuantumJob.__post_init__`
     (`base/quantum_job/dtos.py:230-231`); `iqx_rle` (`utils/serialization.py:19`), `get_duplicates`
     (`utils/general.py:58`)
   - vendored code: `app/libs/qiskit/qobj/utils.py`, imported nowhere; the four `decode_*` but `decode_pulse_qobj`
     (`app/libs/qiskit_providers/utils/json_decoder.py:52, 67, 86, 101`), which leaves `converters.py` dead too
   - `settings.py`: `APP_ROOT_DIR`; and `setup.py`, a bare `setuptools.setup()` that `pyproject.toml` makes redundant
2. **Passwords.** Nothing verifies a password (`verify_password`, `app/services/booking/utils.py:32`, has no caller).
   **Decided:** delete `hash_password`, `verify_password` and `bcrypt` now; `NewUserInfo` takes no password, and MSS's
   is ignored. `User.password` stays, always empty, as existing databases have the column `NOT NULL`, until step 5.4's
   first migration drops it.
3. **Dependencies.** Remove `requests`, `requests-mock`, `qiskit-ibm-provider`, `pytest-asyncio` (the three async tests
   in `test_settings.py` await nothing), the second and third `numpy`, and `httpx` from `dev`, which
   `fastapi[standard]` brings. `networkx` moves to the quantify extra, whose resolution it pins. Declare what is
   imported but not listed: `redis`, `websockets`, `uvicorn`, `starlette`, `pandas`.
   *Done, with departures:* the three tests await the app's lifespan since step 1, so they enter it through
   `TestClient` instead; `psutil` goes too, as `dev/perf/record.sh` brings its own; `xarray`, which the base executor
   code imports, is declared in the quantify extra as in qiskit's; quantify's `numpy>=2.0` stays, as pip reads no lock and
   would keep a qiskit environment's numpy 1.

## Step 3: one shape for the API and the services

1. **Routes out of `app/api/__init__.py`** (796 lines, 25 routes): `app/api/routers/{devices,users,bookings,jobs,
   recalibration,switch,logfiles}.py`, one router per commit, beside step 1.7's test modules. `__init__.py` keeps
   creating the app.
2. **One error table.** The 19 `add_exception_handler` calls (`api/__init__.py:101-119`) and `to_http_error`
   (`app/utils/api.py:174-198`, the only `HTTPException` outside `app/api`) move to `app/api/errors.py`, as one table.
   `PostProcessingError` subclasses `BaseBccException` (`exc.py:67`); `ItemNotFoundError` is imported from
   `utils.exc`, not through `utils.redis_store` (`api/__init__.py:74`); `dependencies.py:362, 381` raise domain errors.
3. **One middleware.** The request id and the request log live in `app/api/middleware.py`, once.
4. **A `devices` service.** `save_all_device_params` and `send_device_params_to_mss`
   (`app/libs/device_parameters/__init__.py:94-186`), the device-property routes' logic (`api/__init__.py:216-231`,
   which call the lib directly) and the device switch (`scheduler/service.py:584-640`) become
   `app/services/devices/`. This ends the cycle `libs/device_parameters` → `services/external/mss`
   (`device_parameters/__init__.py:34, 169`) → `libs/device_parameters` (`mss/dtos.py:19`).
5. **The scheduler's DTOs in the scheduler.** `Job`, `JobStatus`, `Stage` and `JobFile` move from
   `app/libs/queues/dtos.py` to `app/services/scheduler/dtos.py`; `libs/queues` keeps only the queue types.
   *Done in part:* `JobFile` and `LogLevel`, which only the API and the scheduler use, move. `Job`,
   and `Stage`, `JobStatus`, `Timestamps` and `JobResult` it is built from, stay: the waitlist and runner queues store and
   rebuild `Job`, in `_run_next_job` too, which RQ calls with the arguments pickled when it was queued, so the queues
   would have to take the item type, and the jobs queued before an upgrade would come with arguments of the old shape.
   Step 5.5 replaces these queues; the rest moves then. `QueueContext` stays as the queues' context, so the devices
   service, which the scheduler imports, need not import the scheduler. `ExecutorOptions` stays until step 4: its
   `from_settings` reads `settings`, which a service may not import.
6. **Split `app/utils/api.py`.** The response shapes (`PaginatedListResponse`, `TokenResponse`,
   `CancellationDetails`) move to `app/api/`; MSS signature verification and token encryption to
   `app/services/external/mss/auth.py`, beside the signing. Services stop importing the FastAPI-bound module
   (`scheduler/service.py:34`, `mss/dtos.py:21`).
7. **Facades.** `app/services/external/mss/__init__.py`, empty today, re-exports with `__all__`;
   `booking/models.py` becomes `booking/dtos.py`.
   *Done, and further:* the API, the worker, `main.py`, `dev/perf` and the services import the booking, scheduler and
   MSS services through their facades too, each of which exports what is imported through it; tests keep importing the
   internals they patch or prepare.
8. **Imports at the top.** The eight `from .queues import get_queue_pool` inside `tasks.py` (195, 247, 415, 503, 557,
   624, 655, 703) guard no cycle; the local imports in `app/api/worker.py:72-79` and `booking/models.py:124, 256`
   neither. `main.py`'s stay: `BCC_ENV_FILE` must be set before `settings` is imported.

## Step 4: dependencies arrive as arguments

Today every `settings.X` resolves through a module `__getattr__` against a `ContextVar` that the routing middleware
and the worker set (`settings.py:336-338`, with its FIXME), and eight module registries keyed by device or URL hold
the connections. This step makes the device explicit.

1. **`DeviceResources`**, a frozen dataclass of what one device runs on: its settings, Redis connection, SQL engine,
   jobs store, queues, MSS client, executor options, loaded keys and executor lock. The lifespan builds one per device
   from `get_device_settings(name)` and closes them on shutdown, replacing the seven `clear_*` calls
   (`dependencies.py:88-97`); the RQ worker builds its device's at start-up (`app/api/worker.py`).
   *Done in steps:* `DeviceResources` lives in the scheduler, whose tasks take it; until 4.4 its `from_settings` gets
   each piece from its registry, so the routes, the tasks and the registries share one of each, and the `clear_*`
   calls go with their registries.
2. **The API passes it.** `DeviceRoutingMiddleware` (`app/api/device_routing.py:38`) puts the device's name on
   `request.state`; `DeviceResourcesDep` gives a route its device's resources; routes call services with them.
   *Done:* the scheduler service takes `DeviceResources`, and keeps `QueueContext` only where it queues a task, till
   4.3; the devices service, which the scheduler imports, takes the connection, client and device name it uses. The
   device-property routes lose a stray `url` query parameter, which `Depends(get_redis_connection)` exposed.
3. **The tasks are passed it.** RQ tasks take the job's own data; the worker's `perform_job` supplies the device's
   resources. `QueueContext` (`app/libs/queues/dtos.py:569`), a dict of URLs, folders and timeouts handed to every
   task so that each rebuilds engines from URLs (`tasks.py:200`), and copied per request to set `force_normal_queue`
   (`api/__init__.py:535`), goes.
   *Done:* the worker's jobs are `DeviceJob`s, which call their task with the device's resources first, set by the
   worker before it forks, so that the callbacks a dead work horse leaves to it have them too. `force_normal_queue`
   is the preprocessing task's own argument. `BCC_IS_ASYNC`, which ran a job in place, with no worker to give it
   the resources, goes first, in its own commit; the online check is a route dependency.
4. **The registries go**, one per commit, each replaced by its field on `DeviceResources`: `_REDIS_CONNECTIONS`
   (`app/utils/redis.py:20`), `_ENGINE_CACHE` (`app/utils/sql_db.py:23`; closing now disposes the engine),
   `_REQUEST_LOGS_STORES` (`app/utils/api.py:50`), `_QUEUE_CONTEXTS` (`app/libs/queues/dtos.py:77`), `_JOB_STORES`
   (`scheduler/store.py:25`), `_QUEUE_POOLS` (`scheduler/queues.py:30`) with `QueuePool.from_settings`,
   `_EXECUTOR_OPTIONS` and `_QUANTUM_EXECUTORS_AND_OPTS` (`scheduler/utils.py:50-51`), `_MSS_CLIENTS`
   (`mss/service.py:30`). The key caches `_MSS_PUBLIC_KEYS` (`utils/api.py:49`) and `_BCC_PRIVATE_KEYS`
   (`mss/auth.py:24`) give way to the loaded keys.
   *Done, in the reverse order:* each registry's getter fed on the Redis one, so that went last. The request log
   store, an API concern, is kept per device on `app.state` beside the resources; the executor, built by the heavy
   worker alone, is an optional field the worker sets. `close_device_resources` closes the connections, the engine
   and the executor. The lifespan builds a device's resources inside its start-up lock, taken on a connection of
   its own, as building them creates the bookings tables, which API workers starting together would race on.
5. **No `param or settings.X`**, one package per commit: `utils/redis.py:32`, `utils/api.py:275`,
   `utils/compat.py:53, 67`, `base/executor.py:296`, `quantify/executor.py:100-107`, `booking/store.py:29`,
   `booking/service.py:349-354, 377, 461`, `scheduler/store.py:44-52`, `scheduler/utils.py:158, 271`.
   *Done* with 4.6, a commit per package: the executors hold the data directory they are given, and `preprocess`
   and `run` require their folders.
6. **`libs` stops reading `settings`**: `queues/dtos.py:49`, `base/executor.py:24`, `quantify/executor.py:38`, and
   through `utils/compat.py:17` and `utils/logging.py:3`. `ExecutorOptions.from_settings` (`queues/dtos.py:546`)
   moves to the composition root. The booking validators that read `settings` and the clock
   (`booking/models.py:98-137, 220-235, 254-263`) become checks in the booking service, given `BookingsConfig` and
   `now`.
   *Done:* `ExecutorOptions.from_settings` is the scheduler's `get_executor_options`, beside `get_executor`: on the
   DTO, given `DeviceSettings`, it tied `libs` to the app's settings model. The booking checks raise `InvalidBookingError`,
   answered with 422 as the validators' errors were; `DeviceSettings.get_utc_now` gives the time `BCC_CURRENT_DATE`
   stands in for, and `Booking.is_active_at` and `is_complete_at` take it.
7. **Settings without the indirection.** **Decided:** a `pydantic-settings` model, as below. `DeviceSettings` (`settings.py:47`, its `__init__` 276 lines)
   becomes a `pydantic-settings` `BaseSettings` with a source for the `BCC_<PREFIX>_` overrides
   (`settings.py:373-410`), or stays hand-written and split per rule 1. Either way it reads only: the folders it
   creates (`settings.py:95-104`) are created at start-up, `logging.basicConfig` (`:435`) runs in `main.py`, and
   `utils/logging.py:8`'s `print` monkeypatch in test mode goes to the tests' configuration. Then the module
   `__getattr__`, `using_device` and `_CURRENT_DEVICE` are deleted.
   *The model first:* `DeviceResources` holds a device's settings, and services may not import `settings.py`, so
   `DeviceSettings` moves to `app/utils/config.py` as a `pydantic-settings` model before 4.1, its fields snake_case
   and its env names unchanged. `read_device_settings` passes the device's own `BCC_<PREFIX>_<NAME>` variables in as
   init values, which rank over the environment and the file, rather than through `settings_customise_sources`, a
   name the verb rule refuses. Until the rest of this step hands every function its settings, the module
   `__getattr__` reads `settings.X` as the current device's `x`.
   The folders are created by the lifespan and the worker, per device; `set_up_logging` runs in `main.py`'s workers
   and in the lifespan too, as uvicorn's `--workers` import the app without running `main.py`'s commands.
   With the shim gone, `get_device_settings()` without a name is the default device's; the worker and the routes
   are handed theirs, and `preload_worker_globals` takes the settings of the device it loads.

## Step 5: storage

`app/utils/redis_store.py` (669 lines, with `test_store.py`'s 1,049) is a Redis document store with indexes, field
TTLs, pagination and a Lua script, used by three stores of which only one needs more than a key lookup.

1. **Nonces** use step 0.1's `SET NX EX` only; the `RequestLog` collection, which keeps 14 days of every request in an
   indexed hash to answer "seen this nonce?" (`utils/api.py:201`), goes.
2. **Device parameters** (`device_parameters/__init__.py`: `Device`, `DeviceCalibration`, `BackendConfig`, keyed by
   device, never filtered, expired or paged) become one JSON value per device and kind.
3. **Jobs.** **Decide:** the jobs move into the SQL database beside bookings, a table keyed per device and indexed on
   `user_id` and `status`, with Alembic migrations; or they stay in Redis behind a much smaller store. Then
   `redis_store.py`, `test_store.py`, `Schema` with its `__primary_key_fields__`/`__index_fields__` dunders
   (`redis_store.py:79-113`, `device_parameters/dtos.py:47, 61, 645, 873`, `queues/dtos.py:56, 396`) and its
   `model_dump` override go.
   *Decided:* SQL. *Done* after 5.4, so the table comes by migration: `job`, keyed on the device's prefix and
   the job id, holds the `Job` as JSON beside indexed `user_id`, `status` and `updated_at` columns, as `Job`'s
   validators and nested models do not survive an SQLModel table. `JobsStore` keeps the Redis store's few
   operations; a job expires `BCC_JOBS_STORE_TTL` after its last write, deleted as another is inserted, and
   jobs list least recently written first, as Redis's expiry order had them. Jobs in Redis are not copied over.
4. **Schema changes by migration.** `get_sql_engine` stops calling `create_all` on first use (`sql_db.py:52`);
   Alembic owns the bookings and users tables, as it does MSS's.
   *Done:* unlike MSS, which migrates in `start` or `migrate`, the API migrates each device's database as it
   starts, under the start-up lock that `create_all` ran under, as `serve` runs alone in the e2e test. The first
   revision skips a database from before Alembic, whose tables it finds; the second drops `user.password`.
5. **Queues: RQ, or a dispatcher over the jobs.** Both are built, behind the few operations the scheduler needs (claim
   a device's next job, mark its stage, schedule a timed event, cancel), each in commits of its own, and compared;
   the better one stays.
   - **A, RQ used as RQ.** `RunnerQueue` subclasses `rq.Queue` and overrides `enqueue` with another signature
     (`types.py:251`, with an unused `_current_job` at `:308`), and keeps the job and its arguments in hashes of
     its own (`types.py:444-478`) beside RQ's. RQ stores both; `import_func` and `get_function_import_path`
     (`app/libs/queues/utils.py`) give way to `rq.utils.import_attribute`. `StaticQueue` stays a Redis list per
     device, and bookings stay start and end events, idle timers and the waitlist.
     *Done:* the scheduler enqueues its tasks on plain `rq.Queue`s, the postprocessing callbacks with them; an
     execution's estimated duration is its rq job's meta, and a queue's duration sums its queued jobs' and what
     is left of its running one's, from RQ's started registry, instead of a counter beside a copy of each job.
   - **B, a dispatcher over the jobs store**, as qpi-ui's (`internal/scheduler/scheduler.go`): a job is a row with a
     status, and each device's dispatcher claims the next by the booking policy, in one function: the booked
     user's oldest job, else the oldest of anyone's once the booker is idle. A booking's start and end are times
     it compares with now, recalibration a row with a due time, a cancel a status it skips, and a recovery loop
     fails a job running past its timeout. Claiming is atomic in each store: Redis (Lua), SQLite (`BEGIN
     IMMEDIATE`), Postgres (`SKIP LOCKED`), chosen by `BCC_QUEUE_URL`'s scheme, as MSS's `Database` is by its URL.
   Measured on the same machine: step 1's tests, which both must pass unchanged; `dev/perf/`'s p95 latency, stage
   times and peak memory; lines of code; and the workarounds each needs against its library (rule 9).
   **Decide**, from the comparison, which stays; whether B is built at all is decided first. With 5.3's jobs in SQL,
   B's SQLite store comes nearly free.
   *Compared* at `3478d6e`, on a machine whose load average ran from 7 to 32:
   - Tests: A passes both suites. B fails 15 (quantify) and 16 (qiskit). 25 read RQ's own state (all 21 of
     `test_recalibration`, and `test_cancel_job_cancels_its_rq_job`) and would be rewritten for B. The rest are
     policy: B fills a gap before a booking with a job that fits, where A's waitlist is first in, first out; and it
     fails a booker's over-long job only while the booking runs, which the tests' clock has passed.
   - Perf: neither failed a request or a job, once B's results-file race was fixed. B waits about 200 ms (p95) to
     preprocess or postprocess, its 0.2 s idle poll, against A's 122 and 31 ms, and 29 ms to execute against 45.
     The API's p95s moved with the load, not the option: B's two runs were 3% under and 39% over A's aggregate.
     B's peaks are lower: heavy worker 247–491 MiB against 621, light 98–161 against 232, Redis keys about 1,000
     against 1,988.
   - Code: A's own is about 1,330 lines (`libs/queues/types.py`, `queues.py`, `utils/rq.py`, `api/worker.py` and
     its branches in tasks and service); B's about 715, `dispatcher.py` 579 of them.
   - Workarounds: A overrides `DeviceJob._execute` and `PreloadedRqWorker.execute_job`/`check_for_suspension`,
     runs RQ's scheduler in a spawned process, needs `OBJC_DISABLE_INITIALIZE_FORK_SAFETY` on macOS, keeps the
     waitlist in a Lua-scripted list beside RQ and idle timers as repeated jobs, names rq jobs `{job}_{STAGE}`, and
     reads the started registry without cleanup. B limits a stage's time by SIGALRM, main thread only; runs it
     unforked, so a crash takes the worker, which the recovery pass covers; polls; imports `tasks` lazily to break
     a cycle; and registers no RQ worker, so `dev/perf/record.sh` waits for a ready line instead.
   - Redis: A needs it for RQ; under B, item 7 makes it optional.
   *Decided:* B. A goes with RQ, `BCC_SCHEDULER`, `BCC_MAX_GENERAL_QUEUE_TIME`, the jobs' `storage_id` and the
   bookings' rq job ids (migration 0005); `bcc rq` becomes `bcc worker`, which prints a ready line that
   `dev/perf/record.sh` waits for. The tasks return their results and the dispatcher marks each stage done, so they
   no longer call into it; it still imports them lazily, as they load libraries the API need not. A booker's job
   submitted before their booking now runs in it, ahead of others', as `test_submit_long_jobs_before_booking`
   expects.
6. **Wake the dispatchers, not poll them**: the 0.2 s poll is B's added wait between stages. Each
   dispatcher sleeps until its next timed change (`get_next_change_at`, due recalibrations, stage timeouts) or a
   wake-up, a datagram on a Unix socket in its device's folder that the API, after committing a job or cancel, and
   the heavy worker, after an execution, send; a lost one costs a few seconds' fallback poll. Postgres's
   `LISTEN/NOTIFY` could replace the socket there.
   *Done*, but on UDP: a worker binds a port of 127.0.0.1 and names it in `<device>/wakeups/<kind>-<pid>.port`,
   as a Unix socket's path can pass macOS's 104 bytes (`dev/perf/`'s, in a scratch folder, did) and several light
   workers each need their own. The API wakes the heavy worker on a job, a booking's change, a recalibration and
   switching on; the heavy worker wakes the light ones after an execution. A worker waits at most 5 s, or to the
   bookings' next change; due recalibrations and dead workers' jobs wait out the 5 s, instead of being timed.
7. **Redis only for autocalibration**, now that B stays, one store per commit: the device parameters of 5.2, the
   offline flag (`devices/service.py:142`), the MSS nonces of 5.1 (a table with a unique key and an expiry time, whose
   failed insert is the claim `SET NX` was) and the API's start-up lock (`dependencies.py:72`) move to the SQL
   database. Redis is then needed only by tergite-tuner's sessions (`quantify/executor.py:215`), so a qiskit
   device runs without one; once tergite-tuner keeps them in an SQL database, in a change of its own there, the BCC drops
   Redis.
   *Done*, but for the start-up lock, which became a `FileLock` on `<storage root>/.start-up.lock`: it guards the
   migration, so it cannot be a table the migration creates, and SQL's table-less locks differ by database. The
   workers now migrate under it too, as one started beside a new database's API could claim from no table. The
   tables are `device_parameters` (0006), `switched_off_device` (0007) and `mss_nonce` (0008), whose failed insert
   is the claim; `DeviceResources.redis`, `app/utils/redis.py` and the `redis` and `hiredis` dependencies went, as
   tergite-tuner brings `redis` with the quantify extra. Only the quantify test client empties Redis, and a deb
   asks for Redis only for quantify. `BCC_RQ_REDIS_URL` keeps its name, read now only by tergite-tuner, as renaming
   it breaks env files and the packages' provisioning: step 7.3's.

## Step 6: classes, long functions and DTOs

1. **Executors.** `QuantumExecutor` (`base/executor.py:36`) shares `preprocess` and `run` (`:106-232`) by
   inheritance; they become module functions taking the executor, which stays a class for the state it owns.
   `NativeExperiment(ABC, Generic)` (`base/experiment.py:27`), with no abstract members, becomes a plain generic
   dataclass.
   *Done:* `preprocess_qobj` and `run_job`; the test executors' durations wrap the two instead of overriding them.
2. **Simulator backends.** `QiskitPulseBackend(DynamicsBackend)` and its `QiskitPulse1Q`/`QiskitPulse2Q`
   (`qiskit/backends/`) vary a library class through a dozen `generate_*` class-method hooks, four of which only
   return a literal or `None` (`two_qubit.py:63-83`, `one_qubit.py:76`). They become functions returning a
   `DynamicsBackend`, sharing `_get_transmon_params(backend_config)`, which replaces the extraction written twice in
   `two_qubit.py` (97-120, 190-239), and `_get_lo_range(freq)`, written four times.
   *Done:* `get_one_qubit_backend`/`get_two_qubit_backend`; `get_lo_range` is shared in `qiskit/functions.py`.
3. **Quantify instructions.** `BaseInstruction` and its ten subclasses (`quantify/instruction.py:62`) set attributes
   in a loop over `**kwargs` and compare through `filter(lambda)` over `__slots__` (97-121): dataclasses, keeping
   the polymorphic `to_operation`. `register` (`:155`) becomes the registry's `add`; `QuantifyChannelRegistry(dict)`
   (`quantify/channel.py:21`) uses `__missing__` instead of overriding `__getitem__` and `get`.
   *Done*, keyword-only; the square pulse's `ResetClockPhase`, never registered and so never scheduled, went.
4. **Behaviour off DTOs**, one package per commit; `from_*`/`to_*` conversions stay (rule 5):
   - `device_parameters/dtos.py`: `DeviceCalibration.to_toml` writes a file (`:655`), so a pure
     `to_calibration_config()` stays and the write moves to the devices service; the `set_coupling_map` validator
     overwrites two fields (`:817`), which become computed; `_get_local_phases`' rule that the even qubit is the
     control (`:604`) moves to the calibration conversion; `model_dump_unit` and `model_dump_scalar` (`:190, 227`),
     two near-identical walkers, become `to_units` and `to_scalars` over one helper
     *Done*, but the seed file is written by the quantify executor, whose file it is and which alone wrote it,
     not the devices service; `_to_units` and `_to_scalars` are module functions, `to_toml`'s only callers.
   - `scheduler` (after 3.5): `Job.validate_model` sets `actual_duration` and `storage_id`, the latter twice
     (`queues/dtos.py:457-482`), which become computed; `Timestamps.with_updates` (`:258`) moves into its caller.
     (`Job.current_eta` and `Timestamps.current_time_used` went in step 0.4, whose queue keeps the running job's end
     time instead.)
   - `quantum_job`: `QuantumJob.__post_init__` encodes and decodes JSON and overwrites the `local` it was given
     (`base/quantum_job/dtos.py:220`); `is_local` and `metadata` become properties and the serialisation moves to
     `save_job_in_hdf5`. `to_native_qobj_config` (`base/quantum_job/__init__.py:471`) becomes
     `NativeQobjConfig.from_pulse_qobj_config`
   - `booking`: the `generate_*_event_id` validators and `total_duration` overwrite their input
     (`booking/models.py:171-187, 207`); computed fields
   - `quantify`: `QuantifyMetadata.get_clusters` opens hardware connections (`quantify/utils/config.py:192`) and moves
     to the executor
   - class-level dunder maps become module constants: `DeviceEvent.__status_data_map__` (`mss/dtos.py:50`)
   *Done*, but for these: `Job` drops the computed fields from its input, which a dumped job carries; `Booking`
   keeps `total_duration` stored, for sorting by it, filled from `NewBookingInfo`'s computed one; and the clusters
   are created by the quantify executor's `_create_clusters`.
5. **Long functions become compositions**, one per commit, with unit tests for the pure steps (P).

| Function | Lines | Composed of |
| --- | --- | --- |
| `settings.DeviceSettings.__init__` | 276 | step 4.7 |
| `quantify/experiment.py` `_construct_schedule` | 215 | `_get_channel_plans` P, `_get_initial_clock_frequencies` P, `_add_clock_resources`, `_add_root_and_phase_resets`, `_group_by_slot` P, `_align_to_barrier` P, `_add_aligned_operations`, `_add_tail_idles`; `_to_tick(t, interval)` P for the two closures. The `emit` flag is always true (364-368) and a string literal serves as a comment (312-343) |
| `two_qubit.py` `generate_solver` | 95 | `_get_transmon_params` P (6.2), `_get_ladder_operators(dim)` P, `_get_static_hamiltonian` P, `_get_drive_operators` P |
| `QuantifyExecutor.__init__` | 91 | `_get_coordinator`, `_create_coordinator`, `_get_quantum_device` |
| `main.py` `main` | 87 | `_get_argument_parser()` P, `_run_command(args)` |
| `quantum_job` `discriminate_results` | 81 | `_get_sdk_qubit_index_map(calibration)` P, `_discriminate_experiment`, `_get_register` P, `_to_hex_shots` P |
| `quantum_job` `xarray_to_list` | 63 | `_get_ordered_channel_ids` P (narrowing its `except Exception: pass`, 435), `_get_shot_iq_points` P, `_to_iq_point` P |
| `utils/logger.py` `log_Q1ASM_programs` | 61 | `_get_sequencer_programs`, `_log_program_delta` |
| `base/executor.py` `run` | 59 | `_load_expt_metadata`, `_run_experiments`, `_save_results` |
| `train_discriminator` (1Q and 2Q) | 53 | `_fit_lda(iq_0, iq_1)` P and `_get_basis_statevector` P, shared; replaces three closures redefined in the loop |
| `tasks.py` `_try_enqueue_on_booked_queue` | 51 | `_get_usable_booking_time` P, `_enqueue_on_booking` (after 0.11) |
| `QuantifyExecutor.recalibrate` | 50 | `_get_cluster_modes` P, `_recalibrate_cluster`, `_save_calibration_seed` |
| `scheduler` `submit_job_file`, `delete_user_profile` | 44, 36 | the example above; `_cancel_pending_jobs`, `_cancel_incomplete_bookings` |
| `tasks.py` `preprocess`, `execute`, `recalibrate`, `postprocess` | 41-44 | `_read_job_file`, `_estimate_duration`; `_send_calibration_to_mss`, `_schedule_next_recalibration`; `_get_memory` P; one `_fail_job` for the log-and-fail blocks repeated across them |
| `main.py` `_start` | 42 | `_get_worker_processes` P, `_wait_for_first_stop` |

*Done*, but the `DeviceSettings.__init__` row went in 4.7 and `_try_enqueue_on_booked_queue` with RQ in 5.5;
`_get_cluster_modes` is `_get_recalibration_cluster`, tergite-tuner recalibrating through one cluster; there is no
`_schedule_next_recalibration`, the dispatcher scheduling it; and the tasks' log-and-fail blocks are one
`_fail_on_error` context manager. `_construct_schedule`'s schedules and the two-qubit solver's operators were
compared with the old ones' and are the same.

6. **Flatten the hierarchies.** The calibration models, `CalibrationModel(ABC)` → `CalibrationValue`/
   `CalibrationValueSet(ABC)` → qubit, resonator and coupler, plus `DeviceCalibration(Schema, CalibrationValueSet)`,
   with an abstract `from_calib_results` whose signature differs in each subclass: plain models, fields spelled out,
   JSON unchanged. `QDataArray`/`QDataset` subclass xarray only for type parameters, and their `from_xarray` return
   the input (`base/quantum_job/typing.py:69, 96`): type aliases. `PartialMeta`/`create_partial_schema`
   (`app/utils/model.py:34`), which make `Job.validate_model` branch on `isinstance(self, PartialMeta)`, go with the
   partial updates step 5 removes.
   *Done*; the partial schema had gone in step 5. The calibration models each spell out `extra="allow"`, which
   keeps the seed's values beyond the fields, so their JSON is byte for byte the same.
7. **The MSS client.** `MssClient` stays a class for its live websocket; its hand-written backoff loops in `send` and
   `recv` (`mss/service.py`) use `tenacity`, or `websockets`' reconnecting `connect`; `recv` waits the time left, not
   a fresh timeout per call (`:207`).
   *Done*, but with neither: websockets reconnects only in its asyncio client, and tenacity would be a dependency
   for one loop, which `_run_connected` now holds once. `send_event` sends and awaits the reply in it, so an event
   whose connection drops before MSS replies is sent again on the new one, MSS's handlers updating in place.
8. **Job logs.** `log_job_msg` (`scheduler/utils.py:138`, with its FIXME "Why a custom logger") writes ANSI to a file
   by hand: a standard `logging.FileHandler`.
   *Done:* `log_job_error`, ERROR being the only level ever logged.
9. **Last:** remove the `model_dump` overrides that flip defaults (`PaginatedListResponse`, `app/utils/api.py:110`)
   and pass the flags at the call sites. Step 1.1's body tests catch a missed one.
   *Done:* only `/jobs` dumped it; FastAPI's serialiser never called the override for the others.

## Step 7: names, comments and layout

One package per commit; the style-guide baseline (1.9) shrinks with each.

1. **Builders become constructors or getters**: about 22 `generate_*`/`construct_*`/`make_*`, e.g.
   `generate_hardware_map` (`portclock.py:19`, whose `quantify_config` argument is unused), `construct_redis_key`,
   `make_logger`, `_generate_numerical_pulse` (`quantify/instruction.py:744`).
2. **Nouns that say what they hold**: about 25 `data`/`out`/`kind`, e.g. `scheduler/utils.py:254` (the memory),
   `base/quantum_job/__init__.py:312`, `main.py:250` and its `--kind` option (`:136`); and `discriminator_`,
   `qubit_id__`-style names (`scheduler/utils.py:319, 322`, `two_qubit.py:308`).
3. **Names that do not mislead**: `jobs_db`, a Redis collection (`scheduler/utils.py:192, 225, 254`);
   `log_job_failure`, which sets `FAILED` (`fail_job`); `_get_preprocessed_expt_file`, a pickle named `.json`
   (`base/executor.py:285`); `get_executor_and_options`, whose pair `get_quantum_executor` pulls apart (`:115`);
   `_read_devices`, returning (default, all) (`settings.py:413`); `BCC_RQ_REDIS_URL`, now tergite-tuner's Redis, with
   the old name read as well, as the packages' provisioning writes it.
4. **Booleans** start with `is_`/`should_`: about 35, e.g. `reset`, `noise`, `ignore_errors`, `local`, `job_exists`
   (`dependencies.py:275`), `force_normal_queue`, `emit`, `empty_queues`, `reload`, `desc`.
5. **Comments**: the 7 banners (`settings.py:212-214, 243-245`, `quantify/executor.py:119`, `utils/logger.py:62`,
   `tasks.py:115`, `scheduler/utils.py:355`; the vendored files' "ALTERATION NOTICE" is licence text and stays), about
   100 phase labels left after step 6, docstrings that restate their function ("Validate start_utc",
   `booking/models.py:101`; "Converts current to dictionary", `base/quantum_job/dtos.py:168`), the twelve
   `# default: …` comments beside the default (`settings.py:255-306`), string literals in `except` blocks
   (`queues/dtos.py:294, 313, 449`) and a docstring under an assignment (`base/quantum_job/__init__.py:80`).
6. **Clever constructs spelled out**: the `{**…}` merges (`device_parameters/dtos.py:100`, `base/executor.py:277`,
   `base/quantum_job/dtos.py:135`); the dict used as an ordered set (`redis_store.py:194-196`, if it survives step
   5); the symmetric difference filtered by a lambda (`utils/logger.py:241-245`); `dict([(q, i) for …])`
   (`base/quantum_job/__init__.py:314`); the sort key mixing `int` and `str`, a `TypeError` once both appear
   (`:673-682`); `interval` overwritten three times in order (`scheduler/dtos.py:65-91`); a comprehension that rebinds
   `conf` while reading it (`quantify/utils/config.py:238`); `data = data` (`queues/types.py:460`); a property setter
   that defines a new property (`queues/dtos.py:226, 243`).
7. **Layout**: constants to the top (`queues/dtos.py:604`, `redis_store.py:145`); private helpers to the bottom
   (`base/quantum_job/__init__.py:155-273`, `quantify/experiment.py:75-107`, `quantify/utils/calibration.py:28`).
8. **Last: no `app/`.** Its packages, `api`, `libs`, `services`, `utils` and `tests`, move to the root beside
   `main.py`, `settings.py`, `migrations` and `dev`, in one commit that changes only paths and imports
   (`app.x` becomes `x`): the 78 files that name `app.`, Nuitka's `--include-module=app.api`, the Alembic env, the
   packaging and the docs. Checked first, again: no dependency in either extra installs a top-level module of those
   names, which the root's would then shadow or be shadowed by.

## Step 8: close out

1. **`DESIGN.md`** for this repo, in the shape of tergite-frontend's: what the BCC is, its place beside MSS, the
   request and job flows, devices, storage, configuration, deployment, testing and CI, and §"Working on this repo"
   with the conventions above.
2. **Docs that no longer match the code.** `README.md:41` and `CONTRIBUTING.md:102` say the extras can be used
   together "because they conflict" (cannot); `README.md:96-97` swaps the quantify config and metadata files;
   `CONTRIBUTING.md` starts Redis on 6378 without exporting `REDIS_PORT` (116-120), lints with `black` alone
   (122-126) and suggests `-n auto` (137); `docs/configuration.md` names files that do not exist (9, 17-24, 31-38);
   `docs/auth.md` links Bitbucket (4), names `MSS_PRIVATE_KEY_PATH` for `BCC_MSS_PUBLIC_KEY_PATH` (37) and omits nonce
   replay and job tokens; `docs/executors.md:26-28` names a `scripts` folder, a template and an executor factory that
   do not exist; `docs/booking_and_queuing.md` uses conda (35-46), port 5000, and shows `/token` returning a plain JWT.
3. Run tergite-frontend's `dev/e2e_native_test.sh` and the step 1.10 performance check once more against the result.
4. Delete this file; the history keeps it.

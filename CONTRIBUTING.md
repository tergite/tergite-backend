# Contributing to tergite-backend

**This project is not accepting pull requests from the general public yet.**

**It is currently being developed by the core developers only.**

## Government Model

[Chalmers Next Labs AB (CNL)](https://chalmersnextlabs.se) manages and maintains this project on behalf of all contributors.

## Version Control

Tergite is developed on a separate version control system and mirrored publicly on GitHub.
If you are reading this on GitHub, then you are looking at a mirror. 

## Contacting the Tergite Developers

Since the GitHub repositories are only mirrors, no GitHub pull requests or GitHub issue/bug reports 
are looked at. Please get in touch via email <quantum-nextlabs@chalmers.se> instead. 

Take note that the maintainers may not answer every email.

## But We Use [GitHub Flow](https://docs.github.com/en/get-started/quickstart/github-flow), So All Code Changes Happen Through Pull Requests

Pull requests are the best way to propose changes to the codebase (we
use [GitHub Flow](https://docs.github.com/en/get-started/quickstart/github-flow)). We actively welcome your pull
requests:

1. Clone the repo and create your branch from `main`.
2. If you've added code that should be tested, add tests.
3. If you've changed APIs, update the documentation.
4. Ensure the test suite passes.
5. Make sure your code lints.
6. Issue that pull request!

## Any contributions you make will be under the Apache 2.0 software licenses

In short, when you submit code changes, your submissions are understood to be under the
same [Apache 2.0 License](./LICENSE.txt) that covers the project. Feel free to contact the maintainers if that's a concern.

## Write bug reports with detail, background, and sample code

[This is an example](http://stackoverflow.com/q/12488905/180626).
Here's [another example from Craig Hockenberry](http://www.openradar.me/11905408).

**Great Bug Reports** tend to have:

-   A quick summary and/or background
-   Steps to reproduce
    -   Be specific!
    -   Give sample code if you can.
-   What you expected would happen
-   What actually happens
-   Notes (possibly including why you think this might be happening, or stuff you tried that didn't work)

People _love_ thorough bug reports. I'm not even kidding.

## License

By contributing, you agree that your contributions will be licensed under its Apache 2.0 License.

## Contributor Licensing Agreement

Before you can submit any code, all contributors must sign a
contributor license agreement (CLA). By signing a CLA, you're attesting
that you are the author of the contribution, and that you're freely
contributing it under the terms of the Apache-2.0 license.

The [individual CLA](https://tergite.github.io/contributing/icla.pdf) document is available for review as a PDF.

Please note that if your contribution is part of your employment or 
your contribution is the property of your employer, 
you will also most likely need to sign a [corporate CLA](https://tergite.github.io/contributing/ccla.pdf).

All signed CLAs are emails to us at <quantum-nextlabs@chalmers.se>.

## Set up and test

Set up the project as the [README's quick start](./README.md#quick-start) does, up to
`uv sync --dev`, then:

```shell
source .venv/bin/activate
ruff format --check
ruff check
pytest tests -n 4 --dist=worksteal -m "not real_time"
pytest tests -m real_time
```

- `ruff format` and `ruff check --fix` fix what they find. `pre-commit install` runs them on
  every commit.
- Tests marked `real_time` depend on how fast the machine works through jobs against
  bookings, so they run alone, after the rest.
- The two-qubit simulated device's recalibration test runs all of tergite-tuner's nodes and
  takes about three minutes.

### Where the tests are

| Folder | Tests |
| --- | --- |
| `tests/api/` | each router, through the test client |
| `tests/services/` | the executors, the dispatcher and the other services |
| `tests/` | everything else, including `test_migrations.py`, `test_packaging.py` and `test_styleguide.py` |
| `tests/utils/` | shared helpers and fixtures, one file per job: `clients.py`, `workers.py`, `mss.py` (a mock MSS), `executors/`, `jobs.py`, … |

- `tests/conftest.py` calls `setup_test_env()` **before any app import**, because `settings`
  reads the env file when it is imported. Keep that order when adding fixtures.
- `tests/services/test_schedule_snapshots.py` checks that random circuits still play what they
  did before the v2026.11 refactor. `dev/schedule_snapshots/generate.sh` regenerates the
  snapshots.
- `tests/test_styleguide.py` checks the style rules below that a parser can see.
  `tests/fixtures/styleguide_baseline.txt` lists code that broke them when the test was
  added. It only shrinks: a fix deletes its line, and nothing adds one.

### Before a merge request

- the full test suite;
- tergite-frontend's [`dev/e2e_native_test.sh`](https://github.com/tergite/tergite-frontend/blob/main/dev/e2e_native_test.sh)
  against this checkout: `BACKEND_PATH=../tergite-backend ./dev/e2e_native_test.sh`;
- for performance-sensitive changes, `./dev/perf/record.sh <dir>`, then
  `python3 dev/perf/compare.py dev/perf/baselines <dir>`. The numbers are your machine's, so
  on a busy machine record the base commit too and compare the two;
- the change tried on a running BCC with a simulated device and MSS.

CI (GitLab, mirrored by GitHub Actions) runs ruff and the test suite on every push, and builds
the packages on every `v*` tag.

## Where to start

| Task | Where |
| --- | --- |
| New endpoint | a router in `api/routers/`, a service function, and a `tests/api/test_*.py` case |
| New setting | a field on `DeviceSettings` in `utils/config.py` **and** an entry in `.env.example` |
| New table | a SQLModel table in the service's `store.py` or `dtos.py`, imported by `migrations/env.py`, and an Alembic revision |
| New domain error | a subclass in `utils/exc.py`, and its status code in `api/errors.py` |
| New event to MSS | a member of `DeviceEventName` in `services/external/mss/dtos.py`; see [docs/mss_events.md](./docs/mss_events.md#changing-an-events-schema) |
| New job stage or scheduling rule | `services/scheduler/dispatcher.py`; see [docs/dispatcher.md](./docs/dispatcher.md) |
| New executor | [docs/executors.md](./docs/executors.md#adding-an-executor) |

Read [DESIGN.md](./DESIGN.md) first for how the code is laid out.

## Conventions

### Code structure

Code reads like an article: the overview first, the detail below it. New code follows these
rules, and code a change touches is brought in line with them.

1. **A public function is a short list of calls**, each to a function named for the one thing
   it does. Don't extract one-liners, and don't split performance-critical loops.
2. **Callees sit below their callers.** A file runs: constants, public functions in the order
   a caller uses them, then private helpers (`_name`) in the order they are first called.
3. **A comment that labels a phase is a missing function.** `# Construct the pulse schedule`
   becomes the name of a function.
4. **Data is a struct; state gets a class.** Data is a Pydantic model, a frozen `@dataclass`,
   an `Enum` or an `Exception`, with only derived views and `from_*`/`to_*` conversions. A
   component that owns state across calls (a websocket, an instrument coordinator, the
   dispatcher) is a class.
5. **Stateless behaviour is a module-level function** with explicit arguments. No objects that
   only carry other objects, no `Protocol` or ABC with one implementation, no inheritance to
   share behaviour.
6. **Dependencies arrive as arguments.** Nothing reads a global or `settings`, except the
   composition roots (see [DESIGN.md](./DESIGN.md#startup)). Code that needs the database or MSS
   is given the engine, store or client, never a URL.
7. **One home per kind of thing.** Routes in `api/routers/`. `HTTPException` only in `api/`;
   services raise the domain errors of `utils/exc.py`. Imports run `api → services → libs`.
   Dev and test tooling in `dev/`.
8. **Everything a device owns is keyed by the device.** One process serves several devices.
9. **Prefer a reputable library to our own code.** The commit that adds our own says why.
10. **Obvious beats clever.** Plain `if`/`else` over loops over tuples of fields or merged
    dicts. A value that is only defined stays where it is used.

### Style

- **`ruff format` and `ruff check` are authoritative**; CI fails on any finding.
- **Names**: variables are nouns naming their content (`job_filter`, not `data`); functions
  are verbs; booleans start with `is_`/`should_`. A thing that is constructed is a class;
  anything else is got, so `get_*`, not `build_*`/`make_*`.
- **Constants at the top** of the file, after imports.
- **Type hints everywhere.** DTOs are Pydantic models, never bare dicts across a service
  boundary.
- **Docstrings**: one line where one line does; Google style (`Args:`, `Returns:`, `Raises:`)
  where a function takes several arguments or raises domain errors.
- **Comments must earn their place**: the constraint, the gotcha, the *why* the code cannot
  say. Never restate the code or narrate a fix's history.
- **No banner comments** (`# --- section ---`).
- **Tests**: the name says what it tests.
- **Licence header**: every Python file carries the Tergite Apache-2.0 header.

### Commits and changelog

- One logical change per commit. A change to a file that a doc names updates that doc in the
  same commit.
- Every change users or operators notice gets a [CHANGELOG.md](./CHANGELOG.md) entry under
  `[Unreleased]`, in `Added`/`Changed`/`Fixed`: one or two sentences on what changed and what
  an operator would have hit before. The reasoning goes in the commit body.
- A change to how circuits play is also recorded in [docs/how_circuits_run.md](./docs/how_circuits_run.md).

### Releases

Versions are `{year}.{month}.{patch}`, e.g. `2026.06.0`. A release bumps the version in
`pyproject.toml` and `api/__init__.py`, regenerates `uv.lock`, and dates the `[Unreleased]`
section of the changelog.

## References

This document was adapted from [a gist by Brian A. Danielak](https://gist.github.com/briandk/3d2e8b3ec8daf5a27a62) which
was originally adapted from the open-source contribution guidelines
for [Facebook's Draft](https://github.com/facebook/draft-js/blob/a9316a723f9e918afde44dea68b5f9f39b7d9b00/CONTRIBUTING.md)

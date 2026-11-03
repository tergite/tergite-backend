#!/bin/sh
# Builds bcc for this machine's OS, as Nuitka cannot cross-compile: from the repo root,
# `pkg/build.sh`

set -e

uv sync --dev

# spirack's S5k_module has Latin-1 in its comments, which CPython accepts but Nuitka refuses
# to decode, compiling it to a SyntaxError raised whenever spirack is imported
uv run python - <<'EOF'
import importlib.util
import pathlib

spec = importlib.util.find_spec("spirack")
if spec is not None:
    path = pathlib.Path(spec.origin).with_name("S5k_module.py")
    source = path.read_bytes()
    try:
        source.decode("utf-8")
    except UnicodeDecodeError:
        path.write_text(source.decode("latin-1"), encoding="utf-8")
EOF

uv run nuitka --assume-yes-for-downloads main.py

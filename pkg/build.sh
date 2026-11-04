#!/bin/sh
# Builds build/bcc, the folder the packages install as /usr/lib/<package>, or
# /usr/local/lib/<package> on macOS: uv's own build of Python, with the dependencies uv.lock
# pins and the backend's wheel installed into it, and the bcc that runs it. The Python and some
# of the wheels are this machine's OS and architecture's: from the repo root, `pkg/build.sh`

set -e

package=tergite-backend
version=${UV_PYTHON:-3.13}
out=build/bcc
if [ "$(uname -s)" = Darwin ]; then
	installed_at=/usr/local/lib/$package
else
	installed_at=/usr/lib/$package
fi

rm -rf "$out" build/python build/wheel build/requirements.txt
mkdir -p "$out"

# uv's build of Python finds its own files from wherever it is, so it is copied whole, past the
# link of its minor version that uv adds beside it
UV_PYTHON_INSTALL_DIR="$PWD/build/python" uv python install --no-bin "$version"
python=$(UV_PYTHON_INSTALL_DIR="$PWD/build/python" uv python find --managed-python "$version")
cp -R "$(dirname "$(dirname "$(realpath "$python")")")" "$out/python"

uv export --frozen --no-dev --no-emit-project --no-hashes -o build/requirements.txt >/dev/null
# through the sdist, so that the wheel holds nothing a past build left in build/
uv build --out-dir build/wheel >/dev/null
uv pip install --python "$out/python/bin/python3" --break-system-packages --no-deps \
	--link-mode copy -r build/requirements.txt build/wheel/*.whl

# compiled here, as the installed folder is root's, with the paths it is installed at, so that
# a traceback shows the lines of its source
"$out/python/bin/python3" -m compileall -q -j 0 -s "$PWD/$out" -p "$installed_at" \
	"$PWD/$out/python/lib"

cat >"$out/bcc" <<'EOF'
#!/bin/sh
# runs the backend on the Python beside it, isolated from the caller's PYTHON* variables and
# working folder, from which a worker would otherwise import
here=$(dirname "$(realpath "$0")")
exec "$here/python/bin/python3" -I -m main "$@"
EOF
chmod 0755 "$out/bcc"
"$out/bcc" --help >/dev/null
echo "$out"

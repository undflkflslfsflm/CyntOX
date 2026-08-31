#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
"$python_bin" -c 'import sys; assert sys.version_info >= (3, 12)'
if [[ ! -d "$project_root/.venv" ]]; then "$python_bin" -m venv "$project_root/.venv"; fi
"$project_root/.venv/bin/python" -m pip install --disable-pip-version-check 'uv==0.8.14'
"$project_root/.venv/bin/uv" sync --all-groups --locked
"$project_root/.venv/bin/uv" run oslab init --json

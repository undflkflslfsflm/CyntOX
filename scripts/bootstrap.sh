#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
"$python_bin" -c 'import sys; assert sys.version_info >= (3, 12)'
if [[ ! -d "$project_root/.venv" ]]; then "$python_bin" -m venv "$project_root/.venv"; fi
"$project_root/.venv/bin/python" -m pip install --disable-pip-version-check 'uv==0.8.14'
cd "$project_root"
"$project_root/.venv/bin/uv" sync --all-groups --locked
if [[ -f "$project_root/package.json" ]]; then
  if ! command -v pnpm >/dev/null 2>&1; then
    echo "pnpm was not found in PATH or Codex bundled runtime." >&2
    exit 1
  fi
  pnpm install --frozen-lockfile --offline || pnpm install --frozen-lockfile
fi
"$project_root/.venv/bin/uv" run oslab init --json

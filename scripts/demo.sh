#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

oslab_run() {
  if command -v uv >/dev/null 2>&1; then
    uv run oslab "$@"
  else
    python -m oslab.cli "$@"
  fi
}

oslab_run doctor --json
oslab_run build --target fixture --profile debug --json
oslab_run boot --target fixture --seed 1 --json
oslab_run test --target fixture --test fail --seed 1 --json
oslab_run test --target fixture --test crash --seed 1 --json
oslab_run reproduce --mode crash --cold-boots 2 --json
oslab_run fuzz run --campaign-id demo-fixture --seed 101 --iterations 6 --json
oslab_run training dry-run --json
oslab_run report --experiment demo --json
oslab_run integrity check --json

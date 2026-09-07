from __future__ import annotations

import argparse
import os
from pathlib import Path

from oslab.campaign import advance_run
from oslab.database import LabDatabase


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--max-steps", type=int, required=True)
    parser.add_argument("--crash-after", type=int)
    args = parser.parse_args()
    advanced = advance_run(
        LabDatabase(args.database), args.runtime_root, args.run_id, args.max_steps
    )
    if args.crash_after is not None and advanced >= args.crash_after:
        os._exit(97)


if __name__ == "__main__":
    main()

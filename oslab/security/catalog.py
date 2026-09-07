from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def build_catalog(project_root: Path) -> dict[str, Any]:
    catalog = project_root / "awesome-security-agent-harnesses"
    clones = project_root / "linked-repositories"
    urls = _github_urls(catalog)
    entries: list[dict[str, Any]] = []
    for url in urls:
        owner, name = url.removeprefix("https://github.com/").split("/", 1)
        checkout = clones / f"{owner}__{name}"
        entries.append(
            {
                "url": url,
                "commit": _git_head(checkout),
                "checkout_present": (checkout / ".git").exists(),
                "license": _license_name(checkout),
                "integration": _integration_status(owner, name),
            }
        )
    return {
        "schema_version": 1,
        "source": "awesome-security-agent-harnesses",
        "count": len(entries),
        "entries": entries,
        "content_sha256": hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _github_urls(catalog: Path) -> list[str]:
    import re

    pattern = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
    urls: set[str] = set()
    if not catalog.is_dir():
        return []
    for path in catalog.rglob("*.md"):
        urls.update(
            match.rstrip(".,);") for match in pattern.findall(path.read_text(errors="replace"))
        )
    return sorted(urls, key=str.lower)


def _git_head(checkout: Path) -> str | None:
    if not (checkout / ".git").exists():
        return None
    git = shutil.which("git")
    if git is None:
        return None
    result = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments
        [git, "rev-parse", "HEAD"],
        cwd=checkout,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _license_name(checkout: Path) -> str:
    files = [*checkout.glob("LICENSE*"), *checkout.glob("license*")]
    if not files:
        return "unknown"
    text = files[0].read_text(encoding="utf-8", errors="replace")[:2000].lower()
    if "apache license" in text:
        return "Apache-2.0"
    if "mit license" in text:
        return "MIT"
    if "gnu affero" in text:
        return "AGPL"
    if "creative commons" in text or "attribution-sharealike" in text:
        return "CC-BY-SA"
    return "other"


def _integration_status(owner: str, name: str) -> str:
    key = f"{owner}/{name}".lower()
    if key in {
        "openai/codex-security",
        "visa/visa-vulnerability-agentic-harness",
        "google/oss-fuzz-gen",
    }:
        return "optional-adapter"
    return "reference"

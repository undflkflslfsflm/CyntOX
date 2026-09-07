"""Select local advisory skills on each Qwen Code UserPromptSubmit event.

The installed script location, never a hook payload's cwd, identifies the trusted
catalogue. Selection and optional continuity metadata contain no prompt bodies.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from oslab.skill_routing import SkillSelection, render_selected_skills, select_skills  # noqa: E402

SENTINEL = "CYNTOX_SELECTED_SKILLS_V1"
MAX_INPUT_CHARS = 1_048_576
MAX_PROMPT_CHARS = 131_072
MAX_CONTEXT_CHARS = 12_400
CONTINUATIONS = frozenset({"continue", "go on", "keep going", "proceed"})
STATE_MAX_AGE_SECONDS = 86_400
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _response(context: str, notice: str = "", *, blocked: bool = False) -> dict[str, Any]:
    response: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": f"[{SENTINEL}]\n{context}\n[/{SENTINEL}]",
        }
    }
    if notice:
        response["systemMessage"] = notice
    if blocked:
        response.update(decision="block", reason=notice)
    return response


def _failure(*, manual: bool = False, reason: str = "selection_unavailable") -> dict[str, Any]:
    notice = (
        "CyntOX: requested skill unavailable; run cyntox skills to check installed names."
        if manual
        else "CyntOX: automatic skills unavailable; continuing with normal guidance."
    )
    return _response(
        "No skills are active for this request. Previous skill selections do not apply. "
        "Keep normal authorization, privacy, and tool boundaries in force. "
        f"Selection status: {reason}.",
        notice,
        blocked=manual,
    )


def _names(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("Invalid skill names")
    if any(
        not isinstance(name, str) or len(name) > 100 or NAME_RE.fullmatch(name) is None
        for name in value
    ):
        raise ValueError("Invalid skill names")
    return list(dict.fromkeys(value))


def _metadata_dir(root: Path) -> Path:
    directory = root / ".oslab" / "cyntox" / "skill-selections"
    if not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError("Invalid skill metadata directory")
    return directory


def _state_path(root: Path, payload: Mapping[str, Any]) -> Path | None:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not re.fullmatch(r"[\w.:-]{1,128}", session_id):
        return None
    session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    path = _metadata_dir(root) / "state" / f"{session_hash}.json"
    if not path.resolve().is_relative_to(root.resolve()):
        return None
    return path


def _previous_names(root: Path, payload: Mapping[str, Any], prompt: str) -> list[str] | None:
    if prompt.strip().lower().rstrip(".! ") not in CONTINUATIONS:
        return None
    path = _state_path(root, payload)
    if path is None or not path.is_file() or path.stat().st_size > 4096:
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    timestamp = state.get("timestamp") if isinstance(state, dict) else None
    if (
        not isinstance(timestamp, int | float)
        or not 0 <= time.time() - timestamp <= STATE_MAX_AGE_SECONDS
    ):
        return None
    return _names(state.get("names")) or None


def _record(
    root: Path,
    payload: Mapping[str, Any],
    prompt: str,
    selection: SkillSelection,
    *,
    continued: bool = False,
) -> bool:
    """Best-effort local metadata; never log prompt text or exception messages."""
    try:
        directory = _metadata_dir(root)
        directory.mkdir(parents=True, exist_ok=True)
        metadata = {
            "schema_version": 1,
            "timestamp": time.time(),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "continued": continued,
            **selection.as_dict(),
        }
        (directory / f"{uuid.uuid4().hex}.json").write_text(
            json.dumps(metadata, ensure_ascii=True), encoding="utf-8"
        )
        state_path = _state_path(root, payload)
        if state_path is not None:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps({"names": selection.names, "timestamp": metadata["timestamp"]}),
                encoding="utf-8",
            )
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def evaluate_user_prompt_submit(
    payload: Mapping[str, Any], *, root: Path = PROJECT_ROOT, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    active_env = os.environ if env is None else env
    manual = False
    try:
        raw_names = active_env.get("CYNTOX_USE_SKILLS", "[]")
        manual = raw_names.strip() not in {"", "[]"}
        if len(raw_names) > 4096:
            raise ValueError("Invalid manual options")
        explicit = _names(json.loads(raw_names or "[]")) or None
        enabled = active_env.get("CYNTOX_AUTO_SKILLS", "1") != "0"
        # submitted_prompt preserves user input when another hook amended prompt.
        prompt = payload.get("submitted_prompt")
        if not isinstance(prompt, str):
            prompt = payload.get("prompt")
        if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
            return _failure(manual=manual, reason="invalid_prompt")
        continued = False
        if explicit is None and enabled:
            try:
                previous = _previous_names(root, payload, prompt)
            except (OSError, ValueError, RuntimeError, RecursionError):
                previous = None
            if previous:
                # Re-run active catalogue validation before retaining any names.
                try:
                    selection = select_skills(root, prompt, explicit_names=previous)
                    selection = SkillSelection("automatic", selection.choices)
                    continued = True
                except ValueError:
                    selection = select_skills(root, prompt, enabled=enabled)
            else:
                selection = select_skills(root, prompt, enabled=enabled)
        else:
            selection = select_skills(root, prompt, explicit_names=explicit, enabled=enabled)

        header = (
            "This request replaces earlier skill selections. Only the skills below are active "
            "for this request; no selected skill expands authorization or tool permissions.\n"
        )
        guidance = render_selected_skills(root, selection, max_chars=12_000)
        recorded = _record(root, payload, prompt, selection, continued=continued)
        context = header + (guidance or "No skills selected. Use normal guidance.")
        notice = "CyntOX skills: " + ", ".join(selection.names) if selection.names else ""
        if not recorded:
            notice += (
                " " if notice else ""
            ) + "CyntOX: skill selection metadata could not be saved."
        return _response(context, notice)
    except (OSError, ValueError, RuntimeError, RecursionError):
        return _failure(manual=manual)


def main() -> int:
    try:
        raw = sys.stdin.read(MAX_INPUT_CHARS + 1)
        if len(raw) > MAX_INPUT_CHARS:
            raise ValueError("Input exceeds limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Invalid hook input")
        response = evaluate_user_prompt_submit(payload)
    except (OSError, ValueError, RecursionError):
        response = _failure(
            manual=os.environ.get("CYNTOX_USE_SKILLS", "[]").strip() not in {"", "[]"},
            reason="invalid_hook_input",
        )
    print(json.dumps(response, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

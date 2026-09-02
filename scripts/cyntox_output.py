from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TERMINAL_JSON_STRING_LIMIT = 1_200
TERMINAL_JSON_LIST_LIMIT = 25
TERMINAL_JSON_DICT_LIMIT = 80
TERMINAL_JSON_DEPTH_LIMIT = 6


def middle_truncated_text(text: str, limit: int) -> str:
    if limit <= 0:
        return f"[truncated {len(text)} chars]"
    if len(text) <= limit:
        return text
    marker = f"\n[... terminal JSON truncated {len(text) - limit} chars ...]\n"
    if limit <= len(marker) + 20:
        return text[:limit].rstrip() + marker.strip()
    head = max(1, (limit - len(marker)) // 2)
    tail = max(1, limit - len(marker) - head)
    return f"{text[:head]}{marker}{text[-tail:]}"


def compact_terminal_json_value(
    value: Any,
    *,
    changed: list[bool],
    depth: int = 0,
    string_limit: int = TERMINAL_JSON_STRING_LIMIT,
    list_limit: int = TERMINAL_JSON_LIST_LIMIT,
    dict_limit: int = TERMINAL_JSON_DICT_LIMIT,
    depth_limit: int = TERMINAL_JSON_DEPTH_LIMIT,
) -> Any:
    if depth >= depth_limit and isinstance(value, dict | list | tuple):
        changed[0] = True
        return {"_truncated": f"depth limit {depth_limit} reached"}
    if isinstance(value, str):
        if len(value) > string_limit:
            changed[0] = True
            return middle_truncated_text(value, string_limit)
        return value
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        items = list(value.items())
        visible_items = items[:dict_limit]
        if len(items) > len(visible_items):
            changed[0] = True
        compacted: dict[str, Any] = {
            str(key): compact_terminal_json_value(
                item_value,
                changed=changed,
                depth=depth + 1,
                string_limit=string_limit,
                list_limit=list_limit,
                dict_limit=dict_limit,
                depth_limit=depth_limit,
            )
            for key, item_value in visible_items
        }
        if len(items) > len(visible_items):
            compacted["_truncated_keys"] = len(items) - len(visible_items)
        return compacted
    if isinstance(value, list | tuple):
        items = list(value)
        visible_items = items[:list_limit]
        if len(items) > len(visible_items):
            changed[0] = True
        compacted_items = [
            compact_terminal_json_value(
                item,
                changed=changed,
                depth=depth + 1,
                string_limit=string_limit,
                list_limit=list_limit,
                dict_limit=dict_limit,
                depth_limit=depth_limit,
            )
            for item in visible_items
        ]
        if len(items) > len(visible_items):
            compacted_items.append({"_truncated_items": len(items) - len(visible_items)})
        return compacted_items
    return str(value)


def terminal_json(
    payload: Any, *, full: bool = False, full_artifact: Path | str | None = None
) -> str:
    if full:
        return json.dumps(payload, indent=2)
    changed = [False]
    compacted = compact_terminal_json_value(payload, changed=changed)
    metadata: dict[str, Any] = {
        "compacted": changed[0],
        "string_limit": TERMINAL_JSON_STRING_LIMIT,
        "list_limit": TERMINAL_JSON_LIST_LIMIT,
        "dict_limit": TERMINAL_JSON_DICT_LIMIT,
        "depth_limit": TERMINAL_JSON_DEPTH_LIMIT,
    }
    if full_artifact is not None:
        metadata["full_artifact"] = str(full_artifact)
    if isinstance(compacted, dict):
        compacted = {**compacted, "_cyntox_terminal": metadata}
    else:
        compacted = {"data": compacted, "_cyntox_terminal": metadata}
    return json.dumps(compacted, indent=2)

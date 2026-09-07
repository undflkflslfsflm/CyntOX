from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TERMINAL_JSON_STRING_LIMIT = 1_200
TERMINAL_JSON_LIST_LIMIT = 25
TERMINAL_JSON_DICT_LIMIT = 80
TERMINAL_JSON_DEPTH_LIMIT = 6
TERMINAL_JSON_OUTPUT_LIMIT = 12_000
TERMINAL_JSON_STRICT_STRING_LIMIT = 400
TERMINAL_JSON_STRICT_LIST_LIMIT = 10
TERMINAL_JSON_STRICT_DICT_LIMIT = 40
TERMINAL_JSON_STRICT_DEPTH_LIMIT = 4
TERMINAL_JSON_FALLBACK_PREVIEW_LIMIT = 2_000


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


def _terminal_metadata(
    *,
    compacted: bool,
    string_limit: int,
    list_limit: int,
    dict_limit: int,
    depth_limit: int,
    output_limit: int,
    full_artifact: Path | str | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "compacted": compacted,
        "string_limit": string_limit,
        "list_limit": list_limit,
        "dict_limit": dict_limit,
        "depth_limit": depth_limit,
        "output_limit": output_limit,
    }
    if full_artifact is not None:
        metadata["full_artifact"] = str(full_artifact)
    return metadata


def _attach_terminal_metadata(compacted: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    if isinstance(compacted, dict):
        return {**compacted, "_cyntox_terminal": metadata}
    return {"data": compacted, "_cyntox_terminal": metadata}


def _bounded_fallback(
    rendered: str,
    *,
    strict_rendered: str,
    output_limit: int,
    metadata: dict[str, Any],
) -> str:
    fallback_metadata = {
        **metadata,
        "compacted": True,
        "output_truncated": True,
        "first_pass_chars": len(rendered),
        "strict_pass_chars": len(strict_rendered),
    }
    message = (
        "CyntOX terminal JSON exceeded the safe output limit. "
        "Use --json --full redirected to a file when you intentionally need every field."
    )
    preview_limit = min(TERMINAL_JSON_FALLBACK_PREVIEW_LIMIT, max(0, output_limit // 3))
    payload: dict[str, Any] = {
        "summary": message,
        "preview": middle_truncated_text(rendered, preview_limit),
        "_cyntox_terminal": fallback_metadata,
    }
    fallback = json.dumps(payload, indent=2)
    if output_limit > 0 and len(fallback) > output_limit:
        payload.pop("preview", None)
        fallback = json.dumps(payload, indent=2)
    return fallback


def _render_terminal_json(
    payload: Any,
    *,
    string_limit: int,
    list_limit: int,
    dict_limit: int,
    depth_limit: int,
    output_limit: int,
    full_artifact: Path | str | None,
) -> tuple[str, dict[str, Any], Any]:
    changed = [False]
    compacted = compact_terminal_json_value(
        payload,
        changed=changed,
        string_limit=string_limit,
        list_limit=list_limit,
        dict_limit=dict_limit,
        depth_limit=depth_limit,
    )
    metadata = _terminal_metadata(
        compacted=changed[0],
        string_limit=string_limit,
        list_limit=list_limit,
        dict_limit=dict_limit,
        depth_limit=depth_limit,
        output_limit=output_limit,
        full_artifact=full_artifact,
    )
    return json.dumps(_attach_terminal_metadata(compacted, metadata), indent=2), metadata, compacted


def terminal_json(
    payload: Any,
    *,
    full: bool = False,
    full_artifact: Path | str | None = None,
    output_limit: int = TERMINAL_JSON_OUTPUT_LIMIT,
) -> str:
    if full:
        return json.dumps(payload, indent=2)
    rendered, metadata, _compacted = _render_terminal_json(
        payload,
        string_limit=TERMINAL_JSON_STRING_LIMIT,
        list_limit=TERMINAL_JSON_LIST_LIMIT,
        dict_limit=TERMINAL_JSON_DICT_LIMIT,
        depth_limit=TERMINAL_JSON_DEPTH_LIMIT,
        output_limit=output_limit,
        full_artifact=full_artifact,
    )
    if output_limit <= 0 or len(rendered) <= output_limit:
        return rendered

    strict_rendered, strict_metadata, strict_compacted = _render_terminal_json(
        payload,
        string_limit=TERMINAL_JSON_STRICT_STRING_LIMIT,
        list_limit=TERMINAL_JSON_STRICT_LIST_LIMIT,
        dict_limit=TERMINAL_JSON_STRICT_DICT_LIMIT,
        depth_limit=TERMINAL_JSON_STRICT_DEPTH_LIMIT,
        output_limit=output_limit,
        full_artifact=full_artifact,
    )
    strict_metadata = {
        **strict_metadata,
        "output_truncated": True,
        "first_pass_chars": len(rendered),
    }
    strict_rendered = json.dumps(
        _attach_terminal_metadata(strict_compacted, strict_metadata),
        indent=2,
    )
    if len(strict_rendered) <= output_limit:
        return strict_rendered

    return _bounded_fallback(
        rendered,
        strict_rendered=strict_rendered,
        output_limit=output_limit,
        metadata=metadata,
    )

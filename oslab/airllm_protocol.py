from __future__ import annotations

import re


class InvalidAirLlmResponse(ValueError):
    pass


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINK_LIKE_TAG = re.compile(r"<\s*/?\s*([A-Za-z]+)", re.IGNORECASE)


def _looks_like_reasoning_tag(text: str, index: int) -> bool:
    candidate = _THINK_LIKE_TAG.match(text, index)
    if candidate is not None:
        name = candidate.group(1).casefold()
        if name.startswith("think") or "think".startswith(name):
            return True
    remainder = text[index:].casefold()
    return len(remainder) > 1 and any(
        tag.startswith(remainder) for tag in (_THINK_OPEN, _THINK_CLOSE)
    )


def _strip_complete_reasoning_spans(text: str) -> str:
    visible: list[str] = []
    cursor = 0
    visible_start = 0
    inside_reasoning = False
    while (tag_start := text.find("<", cursor)) >= 0:
        remainder = text[tag_start:]
        if remainder.startswith(_THINK_OPEN):
            if inside_reasoning:
                raise InvalidAirLlmResponse("nested reasoning span")
            visible.append(text[visible_start:tag_start])
            inside_reasoning = True
            cursor = tag_start + len(_THINK_OPEN)
            continue
        if remainder.startswith(_THINK_CLOSE):
            if not inside_reasoning:
                raise InvalidAirLlmResponse("misordered reasoning span")
            inside_reasoning = False
            cursor = tag_start + len(_THINK_CLOSE)
            visible_start = cursor
            continue
        if _looks_like_reasoning_tag(text, tag_start):
            raise InvalidAirLlmResponse("malformed reasoning tag")
        cursor = tag_start + 1
    if inside_reasoning:
        raise InvalidAirLlmResponse("incomplete reasoning span")
    visible.append(text[visible_start:])
    return "".join(visible)


def sanitize_response(text: str) -> str:
    """Remove complete hidden-reasoning spans and reject ambiguous or degenerate output."""
    cleaned = _strip_complete_reasoning_spans(text).strip()
    if not cleaned:
        raise InvalidAirLlmResponse("empty final response")
    words = re.findall(r"\S+", cleaned)
    for width in range(1, min(128, len(words) // 4) + 1):
        tail = words[-width:]
        if all(words[-width * repeat : -width * (repeat - 1)] == tail for repeat in (2, 3, 4)):
            raise InvalidAirLlmResponse("repetition loop")
    return cleaned

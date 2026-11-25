from typing import Any

from .history import count_images
from .iotypes import HistoryPayload


# What the Memory panel of the UI (web/memory_editor.js) can do with a stored
# value: a history (a list of messages) or a text are edited, any other value
# (a number, an image...) is only listed.
KIND_HISTORY = "history"
KIND_TEXT = "text"
KIND_OTHER = "other"


class EditError(ValueError):
    """The edited value can not be stored: the message is shown in the panel."""


def kind_of(value: Any) -> str:
    if isinstance(value, HistoryPayload):
        return KIND_HISTORY
    if isinstance(value, str):
        return KIND_TEXT
    return KIND_OTHER


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def summarize(value: Any) -> str:
    """One line describing a stored value, for the list of keys."""
    kind = kind_of(value)
    if kind == KIND_HISTORY:
        messages = value.history or []
        turns = sum(1 for msg in messages if isinstance(msg, dict) and msg.get("role") == "user")
        parts = [_count(turns, "turn"), _count(len(messages), "message")]
        images = count_images(messages)
        if images:
            parts.append(_count(images, "image"))
        return ", ".join(parts)
    if kind == KIND_TEXT:
        return _count(len(value), "character")
    return f"{type(value).__name__}, not editable here"


def to_editable(value: Any) -> Any:
    """The JSON form of a stored value that the panel edits: the list of
    messages of a history, the text itself. None for a value not editable."""
    kind = kind_of(value)
    if kind == KIND_HISTORY:
        return list(value.history or [])
    if kind == KIND_TEXT:
        return value
    return None


def _check_messages(raw: Any) -> list[Any]:
    """Only the shape the Chat Completion node relies on is checked: what a
    message may hold beyond its role and content (the reasoning fields...) is
    kept as it is."""
    if not isinstance(raw, list):
        raise EditError("The history must be a list of messages")
    for number, message in enumerate(raw, start=1):
        where = f"Message {number}"
        if not isinstance(message, dict):
            raise EditError(f"{where} is not an object")
        role = message.get("role")
        if not isinstance(role, str) or not role.strip():
            raise EditError(f"{where} has no role")
        content = message.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list) or not all(
                isinstance(part, dict) and isinstance(part.get("type"), str) for part in content):
            raise EditError(f"{where}: the content must be a text or a list of parts (objects with a type)")
    return raw


def parse_edit(current: Any, raw: Any) -> Any:
    """The value to store, from the JSON form sent by the panel (see
    to_editable) for the kind of the currently stored value. Raises an
    EditError when it is not acceptable."""
    kind = kind_of(current)
    if kind == KIND_HISTORY:
        return HistoryPayload(_check_messages(raw))
    if kind == KIND_TEXT:
        if not isinstance(raw, str):
            raise EditError("The value must be a text")
        return raw
    raise EditError(f"A value of type {type(current).__name__} can not be edited here: only histories and texts can")

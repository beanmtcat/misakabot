"""Validate untrusted JSON once, before it reaches application logic."""

from pydantic import JsonValue, TypeAdapter

_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def json_object(data: str | bytes) -> dict[str, JsonValue]:
    return _OBJECT_ADAPTER.validate_json(data)

"""Canonical identity and strict JSON parsing shared by KV manifest families."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import cast


def canonical_json(value: object, *, allow_nan: bool = True) -> str:
    """Keep canonical encoding identical across IDs and wire envelopes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=allow_nan)


def canonical_digest(value: object) -> str:
    encoded = canonical_json(value).encode()
    return hashlib.sha256(encoded).hexdigest()


def json_string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def json_integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer at least {minimum}")
    return value


def load_json_object(value: str, label: str) -> Mapping[str, object]:
    if type(value) is not str or len(value.encode("utf-8")) > 16 * 1024 * 1024:
        raise ValueError("KV-cache JSON must be text within the 16 MiB wire limit")

    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-finite JSON number is unsupported: {constant}")

    def reject_duplicate_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = item
        return result

    try:
        payload = json.loads(
            value,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_fields,
        )
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must be a JSON object")  # noqa: TRY004
    return cast(Mapping[str, object], payload)

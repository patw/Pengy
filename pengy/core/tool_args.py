"""Schema-driven validation and coercion of tool-call arguments.

Models — small local ones especially — routinely send arguments that are
*almost* right: ``"3"`` for an integer, ``"true"`` for a boolean, a bare
string where a list is expected, or a key in the wrong place.  Before this
module each tool handler read only the keys it knew about, so a misplaced key
(``dry_run`` inside a per-file change) or a mistyped value (``dry_run:
"true"``) was silently replaced by its default — and a "preview" wrote files.

``normalize_arguments()`` runs once in ``execute_tool`` before any handler:

* unambiguous slips are coerced silently (numeric strings, integral floats,
  ``"true"``/``"false"``, one value where an array is expected, a JSON string
  where an array/object is expected, enum values in the wrong case);
* ``null`` is treated as an absent key;
* anything else — an unknown key at any depth, a missing required key, a
  value that can't be coerced — rejects the whole call with an error that
  names the offending key and how to fix it.  Nothing is executed.

Errors are the cheap place to teach the model: they cost tokens only when a
mistake actually happens, unlike tool descriptions which are paid on every
request.  Keep the semantics identical in the Rust and C++ editions
(``normalize_arguments`` in ``tools.rs``, ``Tools::normalizeArguments`` in
``tools.cpp``).
"""

from __future__ import annotations

import json
import re

_INT_RE = re.compile(r"^[+-]?\d+$")
_RAW_KEY = "_raw_arguments"


def _describe(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _preview(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 40 else text[:37] + "..."


def _coerce(schema: dict, value, where: str, errors: list[str], top_keys: set[str]):
    """Return *value* coerced to *schema*, appending problems to *errors*."""
    kind = schema.get("type")

    if kind == "string":
        if isinstance(value, str):
            out = value
        elif isinstance(value, int) and not isinstance(value, bool):
            out = str(value)
        else:
            errors.append(f"{where}: expected a string, got {_describe(value)} {_preview(value)}")
            return None
        enum = schema.get("enum")
        if enum and out not in enum:
            folded = {e.lower(): e for e in enum}
            if out.strip().lower() in folded:
                return folded[out.strip().lower()]
            errors.append(f"{where}: must be one of {', '.join(enum)} (got {_preview(out)})")
            return None
        return out

    if kind == "integer":
        if isinstance(value, bool):
            pass
        elif isinstance(value, int):
            return value
        elif isinstance(value, float) and value.is_integer():
            return int(value)
        elif isinstance(value, str) and _INT_RE.match(value.strip()):
            return int(value.strip())
        errors.append(f"{where}: expected an integer, got {_describe(value)} {_preview(value)}")
        return None

    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        errors.append(f"{where}: expected true or false, got {_describe(value)} {_preview(value)}")
        return None

    if kind == "array":
        if isinstance(value, str) and value.lstrip().startswith("["):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    value = parsed
            except ValueError:
                pass
        if not isinstance(value, list):
            value = [value]
        items = schema.get("items")
        if not items:
            return value
        return [_coerce(items, v, f"{where}[{i}]", errors, top_keys) for i, v in enumerate(value)]

    if kind == "object":
        if isinstance(value, str) and value.lstrip().startswith("{"):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    value = parsed
            except ValueError:
                pass
        if not isinstance(value, dict):
            errors.append(f"{where}: expected an object, got {_describe(value)} {_preview(value)}")
            return None
        return _coerce_object(schema, value, where, errors, top_keys)

    return value


def _coerce_object(schema: dict, value: dict, where: str, errors: list[str],
                   top_keys: set[str]) -> dict:
    props = schema.get("properties")
    if props is None:
        return value
    prefix = f"{where}." if where else ""
    out = {}
    # Keys are visited (and listed) in sorted order: the Rust and C++ editions'
    # JSON maps are sorted, and error text must match across editions.
    for key, raw in sorted(value.items()):
        if key not in props:
            if where and key in top_keys:
                errors.append(f"{prefix}{key}: not allowed here — {key} is a top-level argument")
            else:
                valid = ", ".join(sorted(props)) or "none"
                errors.append(f"{prefix}{key}: unknown key (valid keys: {valid})")
            continue
        if raw is None:
            continue
        out[key] = _coerce(props[key], raw, f"{prefix}{key}", errors, top_keys)
    for key in schema.get("required", []):
        if key not in out:
            errors.append(f"{prefix}{key}: missing required key")
    return out


def normalize_arguments(name: str, arguments, parameters: dict | None) -> tuple[dict, str | None]:
    """Validate *arguments* for tool *name* against its JSON-schema *parameters*.

    Returns ``(coerced_arguments, None)`` on success or ``({}, error_text)``
    when the call must be rejected.  Unknown tools (``parameters is None``)
    pass through untouched so the dispatcher can report them itself.
    """
    if parameters is None:
        return (arguments if isinstance(arguments, dict) else {}), None
    if isinstance(arguments, dict) and set(arguments) == {_RAW_KEY}:
        raw = str(arguments[_RAW_KEY])
        if len(raw) > 200:
            raw = raw[:200] + "..."
        return {}, (
            f"Error: the arguments for {name} were not valid JSON, so nothing was run. "
            f"Resend the call with a single JSON object. Received: {raw}"
        )
    if not isinstance(arguments, dict):
        return {}, f"Error: the arguments for {name} must be a JSON object. Nothing was run."
    errors: list[str] = []
    top_keys = set(parameters.get("properties", {}))
    out = _coerce_object(parameters, arguments, "", errors, top_keys)
    if errors:
        return {}, (
            f"Error: invalid arguments for {name}; nothing was run.\n"
            + "\n".join(f"- {e}" for e in errors)
        )
    return out, None


def parse_raw_arguments(raw: str | None) -> dict:
    """Parse a tool call's raw JSON argument string.

    Accepts an empty string as ``{}`` and one level of double encoding (a JSON
    string containing a JSON object), which some models emit.  Anything that
    still isn't a JSON object is wrapped as ``{"_raw_arguments": raw}`` so
    ``normalize_arguments`` reports it instead of the call running with ``{}``.
    """
    if raw is None or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
        if isinstance(value, str):
            value = json.loads(value)
    except ValueError:
        return {_RAW_KEY: raw}
    return value if isinstance(value, dict) else {_RAW_KEY: raw}

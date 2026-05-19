from __future__ import annotations

import re
from typing import Any


IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BRACKET_REF_RE = re.compile(r"\[([^\]]+)\]")


def evaluate_calculated_columns(record: dict[str, Any], columns: list[dict[str, Any]], label_map: dict[str, str] | None = None) -> dict[str, Any]:
    out = dict(record)
    for column in columns:
        if not _is_active(column):
            continue
        field = str(column.get("field", "")).strip()
        formula = str(column.get("formula", "")).strip()
        if not field or not formula:
            continue
        out[field] = evaluate_formula(formula, out, label_map=label_map)
    return out


def evaluate_formula(formula: str, record: dict[str, Any], label_map: dict[str, str] | None = None) -> Any:
    expr = BRACKET_REF_RE.sub(lambda m: f'col("{m.group(1)}")', formula)

    def col(name: str, default: Any = "") -> Any:
        key = (label_map or {}).get(str(name), str(name))
        return record.get(key, default)

    env: dict[str, Any] = {
        "col": col,
        "num": _num,
        "text": _text,
        "coalesce": _coalesce,
        "ifelse": _ifelse,
        "if_eq": _if_eq,
        "if_in": _if_in,
        "round": round,
        "min": min,
        "max": max,
        "abs": abs,
    }
    for key, value in record.items():
        if IDENT_RE.match(str(key)):
            env[str(key)] = value
    try:
        return eval(expr, {"__builtins__": {}}, env)
    except Exception:
        return ""


def _is_active(column: dict[str, Any]) -> bool:
    value = str(column.get("active", "Y")).strip().upper()
    return value in {"Y", "YES", "TRUE", "1", "是", "启用"}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except Exception:
        return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None and str(value).strip() != "":
            return value
    return ""


def _ifelse(condition: Any, true_value: Any, false_value: Any = "") -> Any:
    return true_value if bool(condition) else false_value


def _if_eq(value: Any, expected: Any, true_value: Any, false_value: Any = "") -> Any:
    return true_value if _text(value).upper() == _text(expected).upper() else false_value


def _if_in(value: Any, options: Any, true_value: Any, false_value: Any = "") -> Any:
    if isinstance(options, str):
        items = {_text(item).upper() for item in options.split(",")}
    else:
        items = {_text(item).upper() for item in options}
    return true_value if _text(value).upper() in items else false_value

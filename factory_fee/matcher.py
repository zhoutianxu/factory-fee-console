from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import pandas as pd

ALL = "ALL"
NA = "N/A"


@dataclass
class MatchOutcome:
    status: str  # MATCHED, NOT_FOUND, DUPLICATED
    rule: dict[str, Any] | None
    priority: int | None
    message: str


def _norm(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _value_matches(data_value: Any, rule_value: Any) -> bool:
    rv = _norm(rule_value)
    dv = _norm(data_value)
    if rv == "":
        # Blank is missing data, not wildcard.
        return False
    if rv.upper() == ALL:
        return True
    if rv.upper() == NA:
        return False
    return rv == dv


def _match_mask(series: pd.Series, data_value: Any) -> pd.Series:
    return series.map(lambda rv: _value_matches(data_value, rv)).astype(bool)


def find_best_match(
    row: pd.Series | dict[str, Any],
    rules: pd.DataFrame,
    priority_specs: list[tuple[int, list[tuple[str, str]]]],
    active_col: str = "is_active",
    use_priority: bool = True,
) -> MatchOutcome:
    if rules.empty:
        return MatchOutcome("NOT_FOUND", None, None, "Rule table is empty")

    active_rules = rules.copy()
    if active_col in active_rules.columns:
        active_rules = active_rules[active_rules[active_col].astype(str).str.upper().eq("Y")]

    row_dict = row.to_dict() if isinstance(row, pd.Series) else dict(row)

    for priority, field_pairs in priority_specs:
        candidates = active_rules.copy()
        for data_field, rule_field in field_pairs:
            if rule_field not in candidates.columns:
                return MatchOutcome("NOT_FOUND", None, priority, f"Missing rule column: {rule_field}")
            data_value = row_dict.get(data_field, "")
            candidates = candidates[_match_mask(candidates[rule_field], data_value)]

        if candidates.empty:
            continue

        if use_priority and "priority" in candidates.columns:
            candidates = candidates[candidates["priority"].astype(str).eq(str(priority))]
            if candidates.empty:
                continue

        if len(candidates) > 1:
            return MatchOutcome(
                "DUPLICATED",
                candidates.iloc[0].to_dict(),
                priority,
                f"Duplicated rules at priority {priority}: {len(candidates)} candidates",
            )

        rule = candidates.iloc[0].to_dict()
        reported_priority = priority
        if not use_priority:
            reported_priority = _rule_priority(rule)
        return MatchOutcome("MATCHED", rule, reported_priority, "Matched")

    return MatchOutcome("NOT_FOUND", None, None, "No matching rule found")


def _rule_priority(rule: dict[str, Any]) -> int | None:
    try:
        value = rule.get("priority")
        if value is None or str(value).strip() == "":
            return None
        return int(float(value))
    except Exception:
        return None


MATERIAL_SPECS = [
    (1, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("location_code", "location_code"), ("movement_type", "movement_type"), ("username", "username"), ("material_code", "material_code")]),
    (2, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("location_code", "location_code"), ("movement_type", "movement_type"), ("material_code", "material_code")]),
    (3, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("material_code", "material_code")]),
    (4, [("yyyymm", "yyyymm"), ("material_code", "material_code")]),
    (9, [("material_code", "material_code")]),
]

MODEL_COEF_SPECS = [
    (1, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("model", "model"), ("factory_material_group", "factory_material_group"), ("process_category", "process_category"), ("shipment_type", "shipment_type")]),
    (2, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("model", "model"), ("factory_material_group", "factory_material_group"), ("process_category", "process_category")]),
    (3, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("model", "model"), ("process_category", "process_category")]),
    (4, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("model", "model")]),
    (9, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("model", "model")]),
]

UNIT_FEE_SPECS = [
    (1, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("model", "model"), ("factory_material_group", "factory_material_group"), ("process_category", "process_category"), ("standard_type", "standard_type")]),
    (2, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("model", "model"), ("process_category", "process_category")]),
    (3, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("model", "model")]),
    (9, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("model", "model")]),
]

PNL_DELIVERY_SPECS = [
    (1, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("model_category", "model_category"), ("process_category", "process_category"), ("factory_material_group", "factory_material_group")]),
    (2, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("model_category", "model_category"), ("process_category", "process_category")]),
    (3, [("yyyymm", "yyyymm"), ("factory_code", "factory_code"), ("model_category", "model_category")]),
    (4, [("yyyymm", "yyyymm"), ("factory_code", "factory_code")]),
    (9, [("factory_code", "factory_code")]),
]

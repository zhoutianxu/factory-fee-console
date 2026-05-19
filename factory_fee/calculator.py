from __future__ import annotations

from typing import Any
import pandas as pd

from .io_files import to_numeric
from .matcher import (
    find_best_match,
    MATERIAL_SPECS,
    MODEL_COEF_SPECS,
    UNIT_FEE_SPECS,
    PNL_DELIVERY_SPECS,
)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except Exception:
        return default


def _delivery_qty_invalid(value: Any) -> bool:
    try:
        if value is None or str(value).strip() == "":
            return True
        return float(value) <= 0
    except Exception:
        return True


def _rule_id(rule: dict[str, Any] | None) -> int | None:
    if not rule:
        return None
    try:
        return int(rule.get("_snapshot_id", rule.get("id", 0)))
    except Exception:
        return None


def _exception(run_id: str, raw: dict[str, Any], stage: str, code: str, message: str, rule_id=None, priority=None) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "import_batch_id": raw.get("import_batch_id"),
        "raw_id": raw.get("raw_id"),
        "yyyymm": raw.get("yyyymm"),
        "factory_code": raw.get("factory_code"),
        "material_code": raw.get("material_code"),
        "exception_stage": stage,
        "exception_code": code,
        "exception_message": message,
        "related_rule_id": rule_id,
        "related_priority": priority,
    }


def calculate(
    run_id: str,
    sap_df: pd.DataFrame,
    material_df: pd.DataFrame,
    model_coef_df: pd.DataFrame,
    unit_fee_df: pd.DataFrame,
    pnl_rule_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    sap = sap_df.copy()
    sap["delivery_qty"] = to_numeric(sap["delivery_qty"], 0.0)

    results: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []

    for _, sap_row in sap.iterrows():
        raw = sap_row.to_dict()
        result = {
            "run_id": run_id,
            "import_batch_id": raw.get("import_batch_id"),
            "raw_id": raw.get("raw_id"),
            "yyyymm": raw.get("yyyymm"),
            "factory_code": raw.get("factory_code"),
            "factory_name": raw.get("factory_name"),
            "movement_type": raw.get("movement_type"),
            "location_code": raw.get("location_code"),
            "username": raw.get("username"),
            "order_no": raw.get("order_no"),
            "legal_entity_code": raw.get("legal_entity_code"),
            "biz_date": raw.get("biz_date"),
            "material_group": raw.get("material_group"),
            "material_code": raw.get("material_code"),
            "material_desc": raw.get("material_desc"),
            "delivery_qty": _num(raw.get("delivery_qty")),
            "calc_status": "SUCCESS",
            "exception_reason": "",
        }
        row_ctx = {**raw, **result}
        exception_messages: list[str] = []

        original_delivery_qty = raw.get("_delivery_qty_original", raw.get("delivery_qty"))
        if _delivery_qty_invalid(original_delivery_qty):
            exceptions.append(_exception(
                run_id,
                raw,
                "raw_sap",
                "DELIVERY_QTY_INVALID",
                f"delivery_qty must be a positive number, got: {original_delivery_qty}",
            ))
            exception_messages.append("DELIVERY_QTY_INVALID")

        # 表2：物料主数据匹配
        mat = find_best_match(row_ctx, material_df, MATERIAL_SPECS)
        if mat.status != "MATCHED":
            code = "MATERIAL_RULE_DUPLICATED" if mat.status == "DUPLICATED" else "MATERIAL_RULE_NOT_FOUND"
            exceptions.append(_exception(run_id, raw, "material_master", code, mat.message, _rule_id(mat.rule), mat.priority))
            exception_messages.append(code)
        else:
            rule = mat.rule or {}
            for field in ["brand", "model", "project_name", "factory_material_group", "model_category", "process_category", "standard_type"]:
                result[field] = rule.get(field, "")
                row_ctx[field] = result[field]
            result["hit_rule_id_material"] = _rule_id(rule)
            result["hit_priority_material"] = mat.priority

        # 表3：机型系数匹配
        coef = find_best_match(row_ctx, model_coef_df, MODEL_COEF_SPECS)
        if coef.status != "MATCHED":
            code = "MODEL_COEF_RULE_DUPLICATED" if coef.status == "DUPLICATED" else "MODEL_COEF_RULE_NOT_FOUND"
            exceptions.append(_exception(run_id, raw, "model_coef", code, coef.message, _rule_id(coef.rule), coef.priority))
            exception_messages.append(code)
        else:
            rule = coef.rule or {}
            result["std_hour_coef"] = _num(rule.get("std_hour_coef"))
            result["difficulty_coef"] = _num(rule.get("difficulty_coef"))
            result["hit_rule_id_model_coef"] = _rule_id(rule)
            result["hit_priority_model_coef"] = coef.priority
            row_ctx.update(result)

        # 表4：单台加工费匹配
        fee = find_best_match(row_ctx, unit_fee_df, UNIT_FEE_SPECS)
        if fee.status != "MATCHED":
            code = "UNIT_FEE_RULE_DUPLICATED" if fee.status == "DUPLICATED" else "UNIT_FEE_RULE_NOT_FOUND"
            exceptions.append(_exception(run_id, raw, "unit_fee", code, fee.message, _rule_id(fee.rule), fee.priority))
            exception_messages.append(code)
        else:
            rule = fee.rule or {}
            unit_fee_local = _num(rule.get("unit_fee_local"))
            currency = str(rule.get("currency_code", "CNY")).upper().strip()
            exchange_rate = _num(rule.get("exchange_rate"), 1.0 if currency == "CNY" else 0.0)
            if currency != "CNY" and exchange_rate == 0:
                exceptions.append(_exception(run_id, raw, "unit_fee", "UNIT_FEE_EXCHANGE_RATE_MISSING", "Non-CNY currency requires exchange_rate", _rule_id(rule), fee.priority))
                exception_messages.append("UNIT_FEE_EXCHANGE_RATE_MISSING")
            unit_fee_cny = unit_fee_local if currency == "CNY" else unit_fee_local * exchange_rate
            result.update({
                "unit_fee_local": unit_fee_local,
                "currency_code": currency,
                "exchange_rate": exchange_rate,
                "unit_fee_cny": unit_fee_cny,
                "hit_rule_id_unit_fee": _rule_id(rule),
                "hit_priority_unit_fee": fee.priority,
            })
            row_ctx.update(result)

        # 表5：损益交货量规则匹配
        pnl = find_best_match(row_ctx, pnl_rule_df, PNL_DELIVERY_SPECS)
        if pnl.status != "MATCHED":
            code = "PNL_DELIVERY_RULE_DUPLICATED" if pnl.status == "DUPLICATED" else "PNL_DELIVERY_RULE_NOT_FOUND"
            exceptions.append(_exception(run_id, raw, "pnl_delivery", code, pnl.message, _rule_id(pnl.rule), pnl.priority))
            exception_messages.append(code)
        else:
            rule = pnl.rule or {}
            include = str(rule.get("include_pnl_delivery", "是")).strip()
            coef_val = _num(rule.get("pnl_delivery_coef"), 1.0)
            pnl_delivery_qty = result["delivery_qty"] * coef_val if include in {"是", "Y", "YES", "1", "TRUE"} else 0.0
            result.update({
                "pnl_delivery_qty": pnl_delivery_qty,
                "hit_rule_id_pnl_delivery": _rule_id(rule),
                "hit_priority_pnl_delivery": pnl.priority,
            })

        # 公式计算
        result["std_hour"] = _num(result.get("std_hour_coef")) * result["delivery_qty"]
        result["difficulty_value"] = _num(result.get("difficulty_coef")) * result["delivery_qty"]
        result["fee_amount_cny"] = _num(result.get("pnl_delivery_qty")) * _num(result.get("unit_fee_cny"))

        if exception_messages:
            result["calc_status"] = "EXCEPTION"
            result["exception_reason"] = ";".join(exception_messages)

        results.append(result)

    return pd.DataFrame(results), pd.DataFrame(exceptions)

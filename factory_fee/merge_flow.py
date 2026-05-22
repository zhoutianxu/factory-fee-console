from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

import pandas as pd
import yaml

from .config import AppConfig
from .db import init_db, write_df
from .formula_engine import evaluate_calculated_columns
from .io_files import normalize_common, read_table
from .matcher import find_best_match

RULE_COLUMN_ALIASES = {
    "yyyymm": ["yyyymm", "年月"],
    "factory_code": ["factory_code", "工厂代码"],
    "factory_name": ["factory_name", "工厂名称"],
    "location_code": ["location_code", "库位"],
    "movement_type": ["movement_type", "移动类型"],
    "username": ["username", "用户名"],
    "material_code": ["material_code", "物料编码"],
    "material_desc": ["material_desc", "物料描述"],
    "brand": ["brand", "品牌"],
    "model": ["model", "机型"],
    "project_name": ["project_name", "项目名称"],
    "factory_material_group": ["factory_material_group", "工厂物料组"],
    "model_category": ["model_category", "机型类别"],
    "process_category": ["process_category", "工艺分类"],
    "standard_type": ["standard_type", "制式"],
    "shipment_type": ["shipment_type", "出货类型"],
    "std_hour_coef": ["std_hour_coef", "标准工时系数"],
    "difficulty_coef": ["difficulty_coef", "综合难度系数"],
    "output_qty": ["output_qty", "产量"],
    "unit_fee_local": ["unit_fee_local", "单台加工费本币"],
    "currency_code": ["currency_code", "货币"],
    "exchange_rate": ["exchange_rate", "汇率"],
    "unit_fee_cny": ["unit_fee_cny", "单台加工费CNY"],
    "include_pnl_delivery": ["include_pnl_delivery", "是否计入损益交货量"],
    "pnl_delivery_coef": ["pnl_delivery_coef", "损益交货量系数"],
    "data_source": ["data_source", "数据来源"],
    "created_at": ["created_at", "创建时间"],
    "created_by": ["created_by", "创建人"],
    "is_active": ["is_active", "是否启用"],
    "priority": ["priority", "优先级"],
}


def make_merge_run_id(flow_key: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"MERGE_{flow_key}_{ts}"


def run_merge_flow(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    import_batch_id: str,
    flow_key: str = "sap_material_master_v1",
    flow_config_path: str | Path | None = None,
    merge_run_id: str | None = None,
    row_limit: int | None = None,
    config_versions: dict[str, str] | None = None,
) -> dict[str, int | str]:
    init_db(conn)
    flow_config_path = Path(flow_config_path or cfg.root_dir / "config" / "merge_flows.yaml")
    flow = _load_flow(flow_config_path, flow_key)
    merge_run_id = merge_run_id or make_merge_run_id(flow_key)
    started_at = datetime.now().isoformat(timespec="seconds")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    conn.execute(
        """
        INSERT OR REPLACE INTO merge_run_log(
            merge_run_id, period_id, flow_key, flow_name, import_batch_id, config_versions_json, status, started_at, message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            merge_run_id,
            cfg.yyyymm,
            flow_key,
            flow.get("name", ""),
            import_batch_id,
            json.dumps(config_versions or {}, ensure_ascii=False),
            "RUNNING",
            started_at,
            "merge started",
        ),
    )
    conn.commit()

    sap = _load_sap_batch(conn, import_batch_id, row_limit=row_limit)
    steps = _flow_steps(flow, flow_key, cfg)
    calculated_columns = _flow_calculated_columns(flow)
    has_calculation_steps = any(_step_action(step) == "calculate" for step in steps)
    label_map = _formula_label_map()

    results: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    db_results: list[dict[str, Any]] = []
    db_exceptions: list[dict[str, Any]] = []

    for _, row in sap.iterrows():
        raw = row.to_dict()
        merged = dict(raw)
        merged.update({"merge_run_id": merge_run_id, "flow_key": flow_key})
        step_exception_codes: list[str] = []
        last_status = "MATCHED"
        last_priority = None
        last_rule_id = None

        for step_index, step in enumerate(steps, start=1):
            action = _step_action(step)
            if action == "calculate":
                columns = _step_calculated_columns(step)
                merged[f"step{step_index}_name"] = step.get("name", f"步骤{step_index}")
                merged[f"step{step_index}_match_status"] = "CALCULATED"
                merged = evaluate_calculated_columns(merged, columns, label_map=label_map)
                continue
            if action != "merge":
                continue
            rule_table_key = str(step.get("right_table", ""))
            rules = _load_rule_table(cfg, rule_table_key, (config_versions or {}).get(rule_table_key))
            priority_specs = _step_priority_specs(step)
            output_fields = [str(field) for field in step.get("output_fields", [])]
            active_col = str(step.get("active_col", flow.get("active_col", "is_active")))
            outcome = find_best_match(merged, rules, priority_specs, active_col=active_col, use_priority=False)
            step_slug = _safe_slug(rule_table_key or f"step_{step_index}")
            merged[f"step{step_index}_name"] = step.get("name", f"步骤{step_index}")
            merged[f"step{step_index}_match_status"] = outcome.status
            merged[f"step{step_index}_hit_priority"] = outcome.priority
            merged[f"step{step_index}_hit_rule_id"] = _rule_id(outcome.rule)
            last_status = outcome.status
            last_priority = outcome.priority
            last_rule_id = _rule_id(outcome.rule)

            if outcome.rule:
                for field in output_fields:
                    merged[field] = outcome.rule.get(field, "")
            else:
                for field in output_fields:
                    merged.setdefault(field, "")

            exception_code = ""
            if outcome.status == "NOT_FOUND":
                exception_code = (
                    "MATERIAL_RULE_NOT_FOUND"
                    if rule_table_key == "table2_material_master"
                    else f"{step_slug.upper()}_NOT_FOUND"
                )
            elif outcome.status == "DUPLICATED":
                exception_code = f"{step_slug.upper()}_DUPLICATED"
            if exception_code:
                step_exception_codes.append(exception_code)
                exception_record = {
                    "merge_run_id": merge_run_id,
                    "period_id": cfg.yyyymm,
                    "flow_key": flow_key,
                    "import_batch_id": import_batch_id,
                    "raw_id": raw.get("raw_id"),
                    "exception_code": exception_code,
                    "exception_message": f"{step.get('name', f'步骤{step_index}')}: {outcome.message}",
                    "hit_priority": outcome.priority,
                    "hit_rule_id": _rule_id(outcome.rule),
                }
                exceptions.append(exception_record)
                db_exceptions.append({
                    **exception_record,
                    "raw_json": json.dumps(merged, ensure_ascii=False, default=str),
                })

        merged["match_status"] = last_status
        merged["hit_priority_material"] = last_priority
        merged["hit_rule_id_material"] = last_rule_id
        merged["merge_exception_reason"] = ";".join(step_exception_codes)
        if not has_calculation_steps:
            merged = evaluate_calculated_columns(merged, calculated_columns, label_map=label_map)

        results.append(merged)
        db_results.append({
            "merge_run_id": merge_run_id,
            "period_id": cfg.yyyymm,
            "flow_key": flow_key,
            "import_batch_id": import_batch_id,
            "raw_id": raw.get("raw_id"),
            "match_status": merged["match_status"],
            "hit_priority": last_priority,
            "hit_rule_id": last_rule_id,
            "raw_json": json.dumps(merged, ensure_ascii=False, default=str),
        })

    result_df = pd.DataFrame(results)
    exception_df = pd.DataFrame(exceptions)
    if db_results:
        write_df(conn, pd.DataFrame(db_results), "merge_result", if_exists="append")
    if db_exceptions:
        write_df(conn, pd.DataFrame(db_exceptions), "merge_exception", if_exists="append")

    result_path = cfg.output_dir / f"merge_result_{merge_run_id}.xlsx"
    exception_path = cfg.output_dir / f"merge_exception_{merge_run_id}.xlsx"
    with pd.ExcelWriter(result_path, engine="openpyxl") as writer:
        result_df.to_excel(writer, index=False, sheet_name="merge_result")
    with pd.ExcelWriter(exception_path, engine="openpyxl") as writer:
        exception_df.to_excel(writer, index=False, sheet_name="merge_exception")

    finished_at = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE merge_run_log
        SET left_rows=?, merged_rows=?, exception_rows=?, status=?, finished_at=?,
            result_file=?, exception_file=?, message=?
        WHERE merge_run_id=?
        """,
        (
            len(sap),
            len(result_df),
            len(exception_df),
            "SUCCESS",
            finished_at,
            str(result_path),
            str(exception_path),
            f"merged_rows={len(result_df)}; exception_rows={len(exception_df)}",
            merge_run_id,
        ),
    )
    conn.commit()
    return {
        "merge_run_id": merge_run_id,
        "flow_key": flow_key,
        "flow_name": str(flow.get("name", "")),
        "import_batch_id": import_batch_id,
        "left_rows": len(sap),
        "merged_rows": len(result_df),
        "exception_rows": len(exception_df),
        "result_file": str(result_path),
        "exception_file": str(exception_path),
    }


def _load_flow(path: Path, flow_key: str) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    flows = raw.get("flows", {})
    if flow_key not in flows:
        raise ValueError(f"Merge flow not found: {flow_key}")
    return dict(flows[flow_key])


def _priority_specs(flow: dict[str, Any]) -> list[tuple[int, list[tuple[str, str]]]]:
    specs = []
    for spec in flow.get("priority_specs", []):
        specs.append((
            int(spec["priority"]),
            [(str(item["left"]), str(item["right"])) for item in spec.get("keys", [])],
        ))
    if not specs:
        raise ValueError("Merge flow priority_specs cannot be empty")
    return specs


def _flow_steps(flow: dict[str, Any], flow_key: str, cfg: AppConfig) -> list[dict[str, Any]]:
    steps = [
        dict(step)
        for step in flow.get("steps", [])
        if step.get("right_table") or step.get("step_action") or step.get("operation")
    ]
    if steps:
        return steps
    legacy_steps = [{
        "name": flow.get("name", "匹配规则表"),
        "right_table": flow.get("right_table"),
        "keys": [
            {"left": left, "right": right}
            for _, pairs in _priority_specs(flow)
            for left, right in pairs
        ],
        "output_fields": flow.get("output_fields", []),
        "active_col": flow.get("active_col", "is_active"),
    }]
    return legacy_steps


def _should_append_default_downstream_steps(flow_key: str, steps: list[dict[str, Any]], cfg: AppConfig) -> bool:
    if flow_key != "sap_material_master_v1" or len(steps) != 1:
        return False
    if str(steps[0].get("right_table", "")) != "table2_material_master":
        return False
    return all(_rule_table_path(cfg, table_key).exists() for table_key in [
        "table3_model_coef",
        "table4_unit_fee",
        "table5_pnl_delivery_rule",
    ])


def _default_downstream_steps() -> list[dict[str, Any]]:
    return [
        {
            "name": "匹配机型系数",
            "right_table": "table3_model_coef",
            "keys": [
                {"left": "yyyymm", "right": "yyyymm"},
                {"left": "factory_code", "right": "factory_code"},
                {"left": "model", "right": "model"},
                {"left": "factory_material_group", "right": "factory_material_group"},
                {"left": "process_category", "right": "process_category"},
                {"left": "shipment_type", "right": "shipment_type"},
            ],
            "output_fields": ["std_hour_coef", "difficulty_coef"],
            "active_col": "is_active",
        },
        {
            "name": "匹配单台加工费",
            "right_table": "table4_unit_fee",
            "keys": [
                {"left": "yyyymm", "right": "yyyymm"},
                {"left": "factory_code", "right": "factory_code"},
                {"left": "model", "right": "model"},
                {"left": "factory_material_group", "right": "factory_material_group"},
                {"left": "process_category", "right": "process_category"},
                {"left": "standard_type", "right": "standard_type"},
            ],
            "output_fields": ["unit_fee_local", "currency_code", "exchange_rate", "unit_fee_cny"],
            "active_col": "is_active",
        },
        {
            "name": "匹配损益交货量",
            "right_table": "table5_pnl_delivery_rule",
            "keys": [
                {"left": "yyyymm", "right": "yyyymm"},
                {"left": "factory_code", "right": "factory_code"},
                {"left": "model_category", "right": "model_category"},
                {"left": "process_category", "right": "process_category"},
                {"left": "factory_material_group", "right": "factory_material_group"},
            ],
            "output_fields": ["include_pnl_delivery", "pnl_delivery_coef"],
            "active_col": "is_active",
        },
    ]


def _step_priority_specs(step: dict[str, Any]) -> list[tuple[int, list[tuple[str, str]]]]:
    keys = step.get("keys", [])
    pairs = [(str(item["left"]), str(item.get("right", item["left"]))) for item in keys if item.get("left")]
    if not pairs:
        raise ValueError(f"Merge step has no keys: {step.get('name', '')}")
    return [(1, pairs)]


def _step_action(step: dict[str, Any]) -> str:
    action = str(step.get("step_action", step.get("operation", "merge"))).strip().lower()
    return action if action in {"merge", "calculate"} else "merge"


def _step_calculated_columns(step: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in step.get("calculated_columns", [])]


def _flow_calculated_columns(flow: dict[str, Any]) -> list[dict[str, Any]]:
    if "calculated_columns" in flow:
        return [dict(item) for item in flow.get("calculated_columns", [])]
    return _default_calculated_columns()


def _default_calculated_columns() -> list[dict[str, Any]]:
    return [
        {
            "field": "pnl_delivery_qty",
            "name": "损益交货量",
            "formula": 'if_in(include_pnl_delivery, "是,Y,YES,1,TRUE", num(delivery_qty) * num(pnl_delivery_coef, 1), 0)',
            "active": "Y",
        },
        {
            "field": "unit_fee_cny",
            "name": "单台加工费CNY",
            "formula": 'if_eq(currency_code, "CNY", num(unit_fee_local), num(unit_fee_local) * num(exchange_rate))',
            "active": "Y",
        },
        {
            "field": "fee_amount_cny",
            "name": "加工费CNY",
            "formula": "num(pnl_delivery_qty) * num(unit_fee_cny)",
            "active": "Y",
        },
        {
            "field": "std_hour",
            "name": "标准工时",
            "formula": "num(std_hour_coef) * num(delivery_qty)",
            "active": "Y",
        },
        {
            "field": "difficulty_value",
            "name": "综合难度",
            "formula": "num(difficulty_coef) * num(delivery_qty)",
            "active": "Y",
        },
    ]


def _formula_label_map() -> dict[str, str]:
    label_map: dict[str, str] = {}
    for canonical, aliases in RULE_COLUMN_ALIASES.items():
        for alias in aliases:
            label_map[str(alias)] = canonical
    label_map.update({
        "交货数量": "delivery_qty",
        "损益交货量": "pnl_delivery_qty",
        "加工费CNY": "fee_amount_cny",
        "标准工时": "std_hour",
        "综合难度": "difficulty_value",
    })
    return label_map


def _safe_slug(value: object) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(value)).strip("_") or "STEP"


def _load_sap_batch(conn: sqlite3.Connection, import_batch_id: str, row_limit: int | None = None) -> pd.DataFrame:
    limit_sql = "LIMIT ?" if row_limit else ""
    params: tuple[Any, ...] = (import_batch_id, int(row_limit)) if row_limit else (import_batch_id,)
    sap = pd.read_sql_query(
        f"""
        SELECT import_batch_id, raw_id, run_id, yyyymm, factory_code, factory_name, movement_type,
               location_code, username, order_no, legal_entity_code, biz_date, material_group,
               material_code, material_desc, delivery_qty_original, delivery_qty, source_table_name,
               source_file_name, imported_at, source_row_no
        FROM raw_sap_monthly_data
        WHERE import_batch_id = ?
        ORDER BY raw_id
        {limit_sql}
        """,
        conn,
        params=params,
    )
    if sap.empty:
        raise ValueError(f"SAP import batch has no rows: {import_batch_id}")
    return sap


def _load_rule_table(cfg: AppConfig, table_key: str, version_id: str | None = None) -> pd.DataFrame:
    path = _rule_table_path(cfg, table_key, version_id=version_id)
    if not path.exists():
        raise ValueError(f"Rule table not found: {table_key}")
    rule = normalize_common(read_table(path)).reset_index(drop=True)
    rule = _normalize_rule_columns(rule)
    rule["_snapshot_id"] = rule.index + 1
    return rule


def _rule_table_path(cfg: AppConfig, table_key: str, version_id: str | None = None) -> Path:
    if version_id:
        version_path = _config_table_version_path(cfg, table_key, version_id)
        if version_path.exists():
            return version_path
    period_path = cfg.root_dir / "data" / "periods" / str(cfg.yyyymm) / "input" / f"{table_key}.csv"
    if period_path.exists():
        return period_path.resolve()
    if table_key in cfg.input_files:
        return cfg.input_files[table_key]
    catalog_path = cfg.root_dir / "config" / "rule_tables.yaml"
    if catalog_path.exists():
        with catalog_path.open("r", encoding="utf-8") as f:
            catalog = yaml.safe_load(f) or {}
        meta = catalog.get("rule_tables", {}).get(table_key, {})
        if meta.get("file"):
            return (cfg.root_dir / str(meta["file"])).resolve()
    return (cfg.root_dir / "data" / "input" / f"{table_key}.csv").resolve()


def _config_table_version_path(cfg: AppConfig, table_key: str, version_id: str) -> Path:
    catalog_path = cfg.root_dir / "config" / "config_table_versions.yaml"
    if catalog_path.exists():
        with catalog_path.open("r", encoding="utf-8") as f:
            catalog = yaml.safe_load(f) or {}
        for version in catalog.get("config_table_versions", {}).get(table_key, []):
            if str(version.get("version_id")) == str(version_id) and version.get("file"):
                return (cfg.root_dir / str(version["file"])).resolve()
    return (cfg.root_dir / "data" / "config_tables" / table_key / f"{version_id}.csv").resolve()


def _normalize_rule_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    normalized_lookup = {_normalize_header(col): col for col in df.columns}
    rename_map = {}
    for canonical, aliases in RULE_COLUMN_ALIASES.items():
        if canonical in df.columns:
            continue
        for alias in aliases:
            source = normalized_lookup.get(_normalize_header(alias))
            if source:
                rename_map[source] = canonical
                break
    return df.rename(columns=rename_map)


def _normalize_header(value: object) -> str:
    return str(value).strip().lower().replace(" ", "").replace("_", "")


def _rule_id(rule: dict[str, Any] | None) -> int | None:
    if not rule:
        return None
    try:
        return int(rule.get("_snapshot_id", rule.get("id", 0)))
    except Exception:
        return None

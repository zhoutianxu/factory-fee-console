from __future__ import annotations

from datetime import datetime
import json
import sqlite3
import pandas as pd

from .config import AppConfig
from .io_files import read_table, normalize_common, to_numeric
from .db import init_db, write_df
from .calculator import calculate
from .merge_flow import _normalize_rule_columns
from .sap_import import import_sap_file


def make_run_id(prefix: str, yyyymm: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{yyyymm}_{ts}"


def _add_snapshot_id(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().reset_index(drop=True)
    df["_snapshot_id"] = df.index + 1
    return df


def _snapshot_rules(conn: sqlite3.Connection, period_id: str, run_id: str, table_name: str, df: pd.DataFrame) -> None:
    records = []
    for i, row in df.reset_index(drop=True).iterrows():
        records.append({
            "period_id": period_id,
            "run_id": run_id,
            "rule_table": table_name,
            "source_row_no": i + 2,
            "raw_json": json.dumps(row.to_dict(), ensure_ascii=False),
        })
    if records:
        pd.DataFrame(records).to_sql("rule_table_snapshot", conn, if_exists="append", index=False)


def _load_sap_batch(conn: sqlite3.Connection, import_batch_id: str, yyyymm: str, factory_scope: list[str]) -> tuple[pd.DataFrame, dict[str, str | int]]:
    batch = conn.execute(
        """
        SELECT import_batch_id, yyyymm, factory_scope, source_file_name, import_rows
        FROM sap_import_batch
        WHERE import_batch_id = ?
        """,
        (import_batch_id,),
    ).fetchone()
    if not batch:
        raise ValueError(f"SAP import batch not found: {import_batch_id}")

    sap = pd.read_sql_query(
        """
        SELECT import_batch_id, raw_id, yyyymm, factory_code, factory_name, movement_type, location_code, username,
               order_no, legal_entity_code, biz_date, material_group, material_code, material_desc,
               delivery_qty_original, delivery_qty, source_table_name, source_file_name, source_row_no
        FROM raw_sap_monthly_data
        WHERE import_batch_id = ?
        ORDER BY raw_id
        """,
        conn,
        params=(import_batch_id,),
    )
    sap = sap.reset_index(drop=True)
    sap["_delivery_qty_original"] = sap["delivery_qty_original"]
    return sap, {
        "import_batch_id": batch[0],
        "sap_batch_yyyymm": batch[1],
        "sap_batch_factory_scope": batch[2] or "",
        "sap_file_name": batch[3] or "",
        "sap_import_rows": int(batch[4] or 0),
    }


def _load_sap_file(conn: sqlite3.Connection, cfg: AppConfig, run_id: str) -> tuple[pd.DataFrame, dict[str, str | int]]:
    summary = import_sap_file(
        conn,
        cfg.input_files["sap_monthly"],
        cfg.yyyymm,
        cfg.factory_scope,
        source_file_name=cfg.input_files["sap_monthly"].name,
        import_batch_id=f"{run_id}_SAP",
    )
    sap, batch_info = _load_sap_batch(conn, str(summary["import_batch_id"]), cfg.yyyymm, cfg.factory_scope)
    return sap, batch_info


def run_pipeline(conn: sqlite3.Connection, cfg: AppConfig, run_id: str | None = None, import_batch_id: str | None = None) -> dict[str, int | str]:
    init_db(conn)
    run_id = run_id or make_run_id(cfg.run_id_prefix, cfg.yyyymm)

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    conn.execute(
        """
        INSERT OR REPLACE INTO calc_run_log(run_id, period_id, import_batch_id, yyyymm, factory_scope, status, started_at, message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, cfg.yyyymm, import_batch_id, cfg.yyyymm, ",".join(cfg.factory_scope), "RUNNING", datetime.now().isoformat(timespec="seconds"), "pipeline started"),
    )
    conn.commit()

    if import_batch_id:
        sap, batch_info = _load_sap_batch(conn, import_batch_id, cfg.yyyymm, cfg.factory_scope)
    else:
        sap, batch_info = _load_sap_file(conn, cfg, run_id)
    sap["run_id"] = run_id
    if "raw_id" not in sap.columns:
        sap["raw_id"] = sap.index + 1
    if "source_row_no" not in sap.columns:
        sap["source_row_no"] = sap.index + 2
    if "_delivery_qty_original" not in sap.columns:
        sap["_delivery_qty_original"] = sap["delivery_qty"]
    sap["delivery_qty"] = to_numeric(sap["delivery_qty"], 0.0)

    material = _add_snapshot_id(_normalize_rule_columns(normalize_common(read_table(cfg.input_files["table2_material_master"]))))
    model_coef = _add_snapshot_id(_normalize_rule_columns(normalize_common(read_table(cfg.input_files["table3_model_coef"]))))
    unit_fee = _add_snapshot_id(_normalize_rule_columns(normalize_common(read_table(cfg.input_files["table4_unit_fee"]))))
    pnl_rule = _add_snapshot_id(_normalize_rule_columns(normalize_common(read_table(cfg.input_files["table5_pnl_delivery_rule"]))))

    # Persist rule snapshots for transparency. Raw SAP data is stored by import_batch_id before calculation.
    for name, df in [
        ("table2_material_master", material),
        ("table3_model_coef", model_coef),
        ("table4_unit_fee", unit_fee),
        ("table5_pnl_delivery_rule", pnl_rule),
    ]:
        _snapshot_rules(conn, cfg.yyyymm, run_id, name, df)

    result_df, exception_df = calculate(run_id, sap, material, model_coef, unit_fee, pnl_rule)
    if not result_df.empty:
        result_df["period_id"] = cfg.yyyymm
    if not exception_df.empty:
        exception_df["period_id"] = cfg.yyyymm
    if not result_df.empty:
        write_df(conn, result_df, "calc_result", if_exists="append")
    if not exception_df.empty:
        write_df(conn, exception_df, "calc_exception", if_exists="append")

    result_path = cfg.output_dir / f"calc_result_{run_id}.xlsx"
    exception_path = cfg.output_dir / f"calc_exception_{run_id}.xlsx"
    with pd.ExcelWriter(result_path, engine="openpyxl") as writer:
        result_df.to_excel(writer, index=False, sheet_name="calc_result")
    with pd.ExcelWriter(exception_path, engine="openpyxl") as writer:
        exception_df.to_excel(writer, index=False, sheet_name="calc_exception")

    success_count = int((result_df.get("calc_status", pd.Series(dtype=str)) == "SUCCESS").sum()) if not result_df.empty else 0
    exception_count = int((result_df.get("calc_status", pd.Series(dtype=str)) == "EXCEPTION").sum()) if not result_df.empty else 0
    conn.execute(
        """
        UPDATE calc_run_log
        SET status=?, finished_at=?, message=?, import_batch_id=?, sap_file_name=?, sap_import_rows=?,
            calc_result_rows=?, calc_exception_rows=?
        WHERE run_id=?
        """,
        (
            "SUCCESS",
            datetime.now().isoformat(timespec="seconds"),
            f"success={success_count}; exception={exception_count}",
            batch_info.get("import_batch_id", import_batch_id or ""),
            batch_info.get("sap_file_name", ""),
            batch_info.get("sap_import_rows", len(sap)),
            len(result_df),
            exception_count,
            run_id,
        ),
    )
    conn.commit()

    return {
        "run_id": run_id,
        "import_batch_id": str(batch_info.get("import_batch_id", import_batch_id or "")),
        "sap_file_name": str(batch_info.get("sap_file_name", "")),
        "sap_import_rows": int(batch_info.get("sap_import_rows", len(sap))),
        "raw_rows": len(sap),
        "result_rows": len(result_df),
        "success_rows": success_count,
        "exception_rows": exception_count,
        "exception_log_rows": len(exception_df),
        "result_file": str(result_path),
        "exception_file": str(exception_path),
    }

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3

import pandas as pd

from .db import init_db, write_df
from .io_files import normalize_common, read_table, to_numeric


RAW_SAP_COLUMNS = [
    "import_batch_id",
    "period_id",
    "raw_id",
    "run_id",
    "yyyymm",
    "factory_code",
    "factory_name",
    "movement_type",
    "location_code",
    "username",
    "order_no",
    "legal_entity_code",
    "biz_date",
    "material_group",
    "material_code",
    "material_desc",
    "delivery_qty_original",
    "delivery_qty",
    "source_table_name",
    "source_file_name",
    "imported_at",
    "source_row_no",
    "source_columns_json",
    "raw_json",
]

SAP_COLUMN_ALIASES = {
    "yyyymm": ["yyyymm", "年月", "期间", "月份", "会计期间", "计算年月"],
    "factory_code": ["factory_code", "工厂代码", "工厂", "工厂编码"],
    "factory_name": ["factory_name", "工厂名称", "工厂名"],
    "movement_type": ["movement_type", "移动类型", "移动类型代码"],
    "location_code": ["location_code", "库位", "库位代码", "库存地点"],
    "username": ["username", "用户名", "用户", "操作人"],
    "order_no": ["order_no", "订单号", "销售订单", "单据号"],
    "legal_entity_code": ["legal_entity_code", "法人编码", "法人代码", "法律实体"],
    "biz_date": ["biz_date", "业务日期", "过账日期", "日期"],
    "material_group": ["material_group", "物料组", "物料组代码"],
    "material_code": ["material_code", "物料编码", "物料代码", "物料", "料号"],
    "material_desc": ["material_desc", "物料描述", "物料名称", "品名"],
    "delivery_qty": ["delivery_qty", "交货数量", "数量", "发货数量", "出货数量"],
    "source_table_name": ["source_table_name", "来源表", "来源表名"],
}


def normalize_yyyymm(value: str) -> str:
    value = str(value).strip()
    if len(value) == 7 and value[4] in {"-", "/"}:
        return value[:4] + value[5:7]
    return value


def make_import_batch_id(yyyymm: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"SAP_{yyyymm}_{ts}"


def import_sap_file(
    conn: sqlite3.Connection,
    file_path: str | Path,
    yyyymm: str,
    factory_scope: list[str],
    source_file_name: str | None = None,
    import_batch_id: str | None = None,
) -> dict[str, str | int]:
    init_db(conn)
    file_path = Path(file_path)
    yyyymm = normalize_yyyymm(yyyymm)
    source_file_name = source_file_name or file_path.name
    import_batch_id = import_batch_id or make_import_batch_id(yyyymm)
    imported_at = datetime.now().isoformat(timespec="seconds")

    raw_sap = normalize_common(read_table(file_path)).reset_index(drop=True)
    source_columns = list(raw_sap.columns)

    sap = normalize_sap_columns(raw_sap)
    sap = sap.reset_index(drop=True)

    if "yyyymm" not in sap.columns:
        sap["yyyymm"] = yyyymm
    else:
        sap["yyyymm"] = sap["yyyymm"].map(normalize_yyyymm)
        sap.loc[sap["yyyymm"].astype(str).str.strip().eq(""), "yyyymm"] = yyyymm

    if "delivery_qty" not in sap.columns:
        sap["delivery_qty"] = ""
    if "source_table_name" not in sap.columns:
        sap["source_table_name"] = ""
    sap["import_batch_id"] = import_batch_id
    sap["period_id"] = yyyymm
    sap["raw_id"] = sap.index + 1
    sap["run_id"] = ""
    sap["source_row_no"] = sap.index + 2
    sap["delivery_qty_original"] = sap["delivery_qty"]
    sap["delivery_qty"] = to_numeric(sap["delivery_qty"], 0.0)
    sap["source_file_name"] = source_file_name
    sap["imported_at"] = imported_at
    sap["source_columns_json"] = json.dumps(source_columns, ensure_ascii=False)
    sap["raw_json"] = [
        json.dumps(record, ensure_ascii=False)
        for record in raw_sap.to_dict("records")
    ]

    for col in RAW_SAP_COLUMNS:
        if col not in sap.columns:
            sap[col] = ""

    write_df(conn, sap[RAW_SAP_COLUMNS], "raw_sap_monthly_data", if_exists="append")
    conn.execute(
        """
        INSERT INTO sap_import_batch(import_batch_id, period_id, yyyymm, factory_scope, source_file_name, import_rows, status, imported_at, message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            import_batch_id,
            yyyymm,
            yyyymm,
            ",".join(factory_scope),
            source_file_name,
            len(sap),
            "SUCCESS",
            imported_at,
            f"loose_import=true; imported_rows={len(sap)}",
        ),
    )
    conn.commit()
    return {
        "import_batch_id": import_batch_id,
        "yyyymm": yyyymm,
        "factory_scope": ",".join(factory_scope),
        "source_file_name": source_file_name,
        "import_rows": len(sap),
        "status": "SUCCESS",
        "imported_at": imported_at,
    }


def _require_columns(df: pd.DataFrame, columns: list[str]) -> None:
    missing = [col for col in columns if col not in df.columns]
    if missing:
        aliases = []
        for col in missing:
            aliases.append(f"{col}({ '/'.join(SAP_COLUMN_ALIASES.get(col, [col])) })")
        raise ValueError(f"缺少 SAP 必填列：{', '.join(aliases)}")


def normalize_sap_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    normalized_lookup = {_normalize_header(col): col for col in df.columns}
    rename_map = {}
    for canonical, aliases in SAP_COLUMN_ALIASES.items():
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

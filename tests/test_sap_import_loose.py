import sqlite3
import json

import pandas as pd

from factory_fee.sap_import import import_sap_file


def test_sap_import_does_not_require_business_columns(tmp_path):
    path = tmp_path / "sap.xlsx"
    pd.DataFrame([
        {"任意字段": "A", "另一列": "10"},
        {"任意字段": "B", "另一列": "20"},
    ]).to_excel(path, index=False)

    conn = sqlite3.connect(":memory:")
    summary = import_sap_file(conn, path, "202502", ["F001", "F002"])

    rows = conn.execute(
        """
        SELECT yyyymm, factory_code, material_code, delivery_qty, source_row_no
        FROM raw_sap_monthly_data
        WHERE import_batch_id = ?
        ORDER BY raw_id
        """,
        (summary["import_batch_id"],),
    ).fetchall()

    assert summary["import_rows"] == 2
    assert rows == [
        ("202502", "", "", 0.0, 2),
        ("202502", "", "", 0.0, 3),
    ]


def test_sap_import_does_not_filter_rows_by_yyyymm_or_factory(tmp_path):
    path = tmp_path / "sap.csv"
    pd.DataFrame([
        {"yyyymm": "202501", "factory_code": "OTHER", "material_code": "MAT1", "delivery_qty": "3"},
        {"yyyymm": "202503", "factory_code": "F999", "material_code": "MAT2", "delivery_qty": "4"},
    ]).to_csv(path, index=False)

    conn = sqlite3.connect(":memory:")
    summary = import_sap_file(conn, path, "202502", ["F001", "F002"])

    rows = conn.execute(
        """
        SELECT yyyymm, factory_code, material_code, delivery_qty
        FROM raw_sap_monthly_data
        WHERE import_batch_id = ?
        ORDER BY raw_id
        """,
        (summary["import_batch_id"],),
    ).fetchall()

    assert summary["import_rows"] == 2
    assert rows == [
        ("202501", "OTHER", "MAT1", 3.0),
        ("202503", "F999", "MAT2", 4.0),
    ]


def test_sap_import_preserves_source_headers_for_preview(tmp_path):
    path = tmp_path / "sap.xlsx"
    pd.DataFrame([
        {"工厂": "1051", "移动类型": "101", "订单号": "1000001723", "自定义列": "原值"},
    ]).to_excel(path, index=False)

    conn = sqlite3.connect(":memory:")
    summary = import_sap_file(conn, path, "202502", [])

    source_columns_json, raw_json = conn.execute(
        """
        SELECT source_columns_json, raw_json
        FROM raw_sap_monthly_data
        WHERE import_batch_id = ?
        """,
        (summary["import_batch_id"],),
    ).fetchone()

    assert json.loads(source_columns_json) == ["工厂", "移动类型", "订单号", "自定义列"]
    assert json.loads(raw_json) == {
        "工厂": "1051",
        "移动类型": "101",
        "订单号": "1000001723",
        "自定义列": "原值",
    }

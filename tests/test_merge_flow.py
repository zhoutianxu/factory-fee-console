import sqlite3
from pathlib import Path

import pandas as pd

from factory_fee.config import AppConfig
from factory_fee.db import init_db
from factory_fee.merge_flow import run_merge_flow


def test_configurable_sap_material_merge_outputs_result_and_exception(tmp_path):
    cfg = _make_cfg(tmp_path)
    pd.DataFrame([
        {
            "年月": "202502",
            "工厂代码": "F001",
            "库位": "L1",
            "移动类型": "101",
            "用户名": "u1",
            "物料编码": "MAT001",
            "品牌": "TECNO",
            "机型": "M1",
            "项目名称": "ProjectA",
            "工厂物料组": "主板",
            "机型类别": "单机头",
            "工艺分类": "组装",
            "制式": "4G",
            "是否启用": "Y",
            "优先级": "1",
        }
    ]).to_csv(cfg.input_files["table2_material_master"], index=False)

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    conn.executemany(
        """
        INSERT INTO raw_sap_monthly_data(
            import_batch_id, raw_id, run_id, yyyymm, factory_code, location_code,
            movement_type, username, material_code, delivery_qty
        )
        VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("SAP_TEST", 1, "202502", "F001", "L1", "101", "u1", "MAT001", 3),
            ("SAP_TEST", 2, "202502", "F001", "L1", "101", "u1", "MAT404", 5),
        ],
    )

    flow_config = tmp_path / "merge_flows.yaml"
    flow_config.write_text(
        """
flows:
  sap_material_master_v1:
    name: "表1 SAP流水合并表2物料主数据"
    right_table: "table2_material_master"
    active_col: "is_active"
    priority_specs:
      - priority: 1
        keys:
          - {left: "yyyymm", right: "yyyymm"}
          - {left: "factory_code", right: "factory_code"}
          - {left: "location_code", right: "location_code"}
          - {left: "movement_type", right: "movement_type"}
          - {left: "username", right: "username"}
          - {left: "material_code", right: "material_code"}
    output_fields:
      - "brand"
      - "model"
    unmatched: "mark_exception"
""",
        encoding="utf-8",
    )

    summary = run_merge_flow(
        conn,
        cfg,
        import_batch_id="SAP_TEST",
        merge_run_id="MERGE_TEST",
        flow_config_path=flow_config,
    )

    assert summary["merged_rows"] == 2
    assert summary["exception_rows"] == 1
    result_rows = conn.execute(
        "SELECT match_status, hit_priority FROM merge_result WHERE merge_run_id = ? ORDER BY raw_id",
        ("MERGE_TEST",),
    ).fetchall()
    exception_rows = conn.execute(
        "SELECT exception_code FROM merge_exception WHERE merge_run_id = ?",
        ("MERGE_TEST",),
    ).fetchall()
    assert result_rows == [("MATCHED", 1), ("NOT_FOUND", None)]
    assert exception_rows == [("MATERIAL_RULE_NOT_FOUND",)]
    assert Path(summary["result_file"]).exists()
    assert Path(summary["exception_file"]).exists()


def test_merge_flow_runs_multiple_steps(tmp_path):
    cfg = _make_cfg(tmp_path)
    pd.DataFrame([{
        "yyyymm": "202502",
        "factory_code": "F001",
        "material_code": "MAT001",
        "model": "M1",
        "process_category": "组装",
        "is_active": "Y",
        "priority": "1",
    }]).to_csv(cfg.input_files["table2_material_master"], index=False)
    pd.DataFrame([{
        "yyyymm": "202502",
        "factory_code": "F001",
        "model": "M1",
        "process_category": "组装",
        "std_hour_coef": "1.2",
        "difficulty_coef": "1.1",
        "is_active": "Y",
        "priority": "1",
    }]).to_csv(cfg.input_files["table3_model_coef"], index=False)

    flow_config = tmp_path / "merge_flows.yaml"
    flow_config.write_text(
        """
flows:
  multi_step:
    name: "多步骤测试"
    left_table: "raw_sap_monthly_data"
    right_table: "table2_material_master"
    active_col: "is_active"
    steps:
      - name: "匹配物料"
        right_table: "table2_material_master"
        keys:
          - {left: "yyyymm", right: "yyyymm"}
          - {left: "factory_code", right: "factory_code"}
          - {left: "material_code", right: "material_code"}
        output_fields: ["model", "process_category"]
      - name: "匹配系数"
        right_table: "table3_model_coef"
        keys:
          - {left: "yyyymm", right: "yyyymm"}
          - {left: "factory_code", right: "factory_code"}
          - {left: "model", right: "model"}
          - {left: "process_category", right: "process_category"}
        output_fields: ["std_hour_coef", "difficulty_coef"]
""",
        encoding="utf-8",
    )

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    conn.execute(
        """
        INSERT INTO raw_sap_monthly_data(
            import_batch_id, raw_id, run_id, yyyymm, factory_code, material_code, delivery_qty
        )
        VALUES ('SAP_TEST', 1, '', '202502', 'F001', 'MAT001', 3)
        """
    )

    summary = run_merge_flow(
        conn,
        cfg,
        import_batch_id="SAP_TEST",
        flow_key="multi_step",
        merge_run_id="MERGE_MULTI",
        flow_config_path=flow_config,
    )

    raw_json = conn.execute(
        "SELECT raw_json FROM merge_result WHERE merge_run_id = 'MERGE_MULTI'"
    ).fetchone()[0]
    assert summary["exception_rows"] == 0
    assert '"model": "M1"' in raw_json
    assert '"std_hour_coef": "1.2"' in raw_json


def test_configured_merge_ignores_hidden_priority_filter(tmp_path):
    cfg = _make_cfg(tmp_path)
    pd.DataFrame([{
        "yyyymm": "202502",
        "factory_code": "F001",
        "material_code": "MAT001",
        "model": "M1",
        "is_active": "Y",
        "priority": "9",
    }]).to_csv(cfg.input_files["table2_material_master"], index=False)

    flow_config = tmp_path / "merge_flows.yaml"
    flow_config.write_text(
        """
flows:
  configured_only:
    name: "按界面字段匹配"
    steps:
      - name: "匹配物料"
        right_table: "table2_material_master"
        keys:
          - {left: "material_code", right: "material_code"}
        output_fields: ["model"]
""",
        encoding="utf-8",
    )

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    conn.execute(
        """
        INSERT INTO raw_sap_monthly_data(
            import_batch_id, raw_id, run_id, yyyymm, factory_code, material_code, delivery_qty
        )
        VALUES ('SAP_TEST', 1, '', '202502', 'F001', 'MAT001', 3)
        """
    )

    summary = run_merge_flow(
        conn,
        cfg,
        import_batch_id="SAP_TEST",
        flow_key="configured_only",
        merge_run_id="MERGE_PRIORITY_IGNORED",
        flow_config_path=flow_config,
    )

    raw_json = conn.execute(
        "SELECT raw_json FROM merge_result WHERE merge_run_id = 'MERGE_PRIORITY_IGNORED'"
    ).fetchone()[0]
    assert summary["exception_rows"] == 0
    assert '"model": "M1"' in raw_json


def test_merge_flow_applies_configured_calculated_columns(tmp_path):
    cfg = _make_cfg(tmp_path)
    pd.DataFrame([{
        "material_code": "MAT001",
        "unit_fee_local": "2",
        "is_active": "Y",
        "priority": "9",
    }]).to_csv(cfg.input_files["table2_material_master"], index=False)

    flow_config = tmp_path / "merge_flows.yaml"
    flow_config.write_text(
        """
flows:
  calc_columns:
    name: "计算列测试"
    steps:
      - name: "匹配物料"
        right_table: "table2_material_master"
        keys:
          - {left: "material_code", right: "material_code"}
        output_fields: ["unit_fee_local"]
    calculated_columns:
      - field: "fee_amount_cny"
        name: "加工费CNY"
        formula: "num(delivery_qty) * num(unit_fee_local)"
        active: "Y"
""",
        encoding="utf-8",
    )

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    conn.execute(
        """
        INSERT INTO raw_sap_monthly_data(
            import_batch_id, raw_id, run_id, yyyymm, factory_code, material_code, delivery_qty
        )
        VALUES ('SAP_TEST', 1, '', '202502', 'F001', 'MAT001', 3)
        """
    )

    run_merge_flow(
        conn,
        cfg,
        import_batch_id="SAP_TEST",
        flow_key="calc_columns",
        merge_run_id="MERGE_CALC_COLUMNS",
        flow_config_path=flow_config,
    )

    raw_json = conn.execute(
        "SELECT raw_json FROM merge_result WHERE merge_run_id = 'MERGE_CALC_COLUMNS'"
    ).fetchone()[0]
    assert '"fee_amount_cny": 6.0' in raw_json


def _make_cfg(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    return AppConfig(
        root_dir=tmp_path,
        database_path=tmp_path / "factory_fee.db",
        output_dir=output_dir,
        input_files={
            "sap_monthly": input_dir / "sap.csv",
            "table2_material_master": input_dir / "table2.csv",
            "table3_model_coef": input_dir / "table3.csv",
            "table4_unit_fee": input_dir / "table4.csv",
            "table5_pnl_delivery_rule": input_dir / "table5.csv",
        },
        yyyymm="202502",
        factory_scope=["F001"],
    )

import pandas as pd
import yaml

from factory_fee.web_app import create_app, _merge_flow_config, _order_rule_df_for_storage, _rule_export_df, _table_preview


def test_rule_preview_shows_chinese_labels_for_known_internal_columns(tmp_path):
    path = tmp_path / "table2_material_master.csv"
    pd.DataFrame([{
        "yyyymm": "202502",
        "factory_code": "F001",
        "material_code": "MAT001",
        "priority": "1",
    }]).to_csv(path, index=False)

    preview = _table_preview(path, "table2_material_master")

    assert preview["columns"] == ["年月", "工厂代码", "物料编码", "优先级"]
    assert preview["rows"][0]["工厂代码"] == "F001"


def test_rule_preview_keeps_chinese_headers_and_places_standard_columns_first(tmp_path):
    path = tmp_path / "rule.xlsx"
    pd.DataFrame([{
        "自定义字段": "A",
        "工厂代码": "F001",
    }]).to_excel(path, index=False)

    preview = _table_preview(path, "table2_material_master")

    assert preview["columns"] == ["工厂代码", "自定义字段"]


def test_rule_export_uses_chinese_standard_columns_and_appends_extra_fields(tmp_path):
    path = tmp_path / "table3_model_coef.csv"
    pd.DataFrame([{
        "自定义字段": "X",
        "model": "M1",
        "yyyymm": "202502",
        "factory_code": "F001",
    }]).to_csv(path, index=False)

    export_df = _rule_export_df(path, "table3_model_coef")

    assert list(export_df.columns) == ["年月", "工厂代码", "机型", "自定义字段"]


def test_rule_upload_storage_orders_extra_fields_after_standard_columns():
    df = pd.DataFrame([{
        "自定义字段": "X",
        "机型": "M1",
        "年月": "202502",
        "工厂代码": "F001",
    }])

    ordered_df = _order_rule_df_for_storage(df, "table3_model_coef")

    assert list(ordered_df.columns) == ["年月", "工厂代码", "机型", "自定义字段"]


def test_rule_download_returns_xlsx_with_chinese_headers(tmp_path):
    config_dir = tmp_path / "config"
    input_dir = tmp_path / "data" / "input"
    config_dir.mkdir()
    input_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        """
app:
  database_path: "data/test.db"
  output_dir: "data/output"
input_files:
  sap_monthly: "data/input/sap.csv"
  table2_material_master: "data/input/table2_material_master.csv"
  table3_model_coef: "data/input/table3_model_coef.csv"
  table4_unit_fee: "data/input/table4_unit_fee.csv"
  table5_pnl_delivery_rule: "data/input/table5_pnl_delivery_rule.csv"
run:
  yyyymm: "202502"
  factory_scope: []
""",
        encoding="utf-8",
    )
    pd.DataFrame([{
        "yyyymm": "202502",
        "factory_code": "F001",
        "material_code": "MAT001",
    }]).to_csv(input_dir / "table2_material_master.csv", index=False)

    app = create_app(config_dir / "config.yaml")
    response = app.test_client().get("/rules/table2_material_master/download")

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "table2_material_master.xlsx" in response.headers["Content-Disposition"]


def test_can_create_custom_rule_table_and_select_it_for_merge(tmp_path):
    config_dir = tmp_path / "config"
    input_dir = tmp_path / "data" / "input"
    config_dir.mkdir()
    input_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        """
app:
  database_path: "data/test.db"
  output_dir: "data/output"
input_files:
  sap_monthly: "data/input/sap.csv"
  table2_material_master: "data/input/table2_material_master.csv"
  table3_model_coef: "data/input/table3_model_coef.csv"
  table4_unit_fee: "data/input/table4_unit_fee.csv"
  table5_pnl_delivery_rule: "data/input/table5_pnl_delivery_rule.csv"
run:
  yyyymm: "202502"
  factory_scope: []
""",
        encoding="utf-8",
    )
    app = create_app(config_dir / "config.yaml")
    client = app.test_client()

    response = client.post(
        "/rules/create",
        data={"table_key": "custom_map", "name": "自定义映射表", "description": "测试"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert (config_dir / "rule_tables.yaml").exists()

    response = client.post(
        "/merge/material/config",
        data={
            "rule_table": "custom_map",
            "match_fields": ["material_code"],
            "return_to": "rules",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    config_text = (config_dir / "merge_flows.yaml").read_text(encoding="utf-8")
    assert "right_table: custom_map" in config_text


def test_can_create_new_merge_flow_from_ui(tmp_path):
    config_dir = tmp_path / "config"
    input_dir = tmp_path / "data" / "input"
    config_dir.mkdir()
    input_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        """
app:
  database_path: "data/test.db"
  output_dir: "data/output"
input_files:
  sap_monthly: "data/input/sap.csv"
  table2_material_master: "data/input/table2_material_master.csv"
  table3_model_coef: "data/input/table3_model_coef.csv"
  table4_unit_fee: "data/input/table4_unit_fee.csv"
  table5_pnl_delivery_rule: "data/input/table5_pnl_delivery_rule.csv"
run:
  yyyymm: "202502"
  factory_scope: []
""",
        encoding="utf-8",
    )
    app = create_app(config_dir / "config.yaml")
    response = app.test_client().post(
        "/merge/flows/create",
        data={
            "flow_key": "monthly_fee_flow",
            "name": "加工费月度计算流程",
            "description": "测试流程",
            "main_table": "raw_sap_monthly_data",
            "right_table": "table2_material_master",
            "step_name": "匹配物料主数据",
            "match_fields": ["material_code", "factory_code"],
            "output_fields": ["brand", "model"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    config_text = (config_dir / "merge_flows.yaml").read_text(encoding="utf-8")
    assert "monthly_fee_flow:" in config_text
    assert "right_table: table2_material_master" in config_text


def test_can_create_merge_flow_with_more_than_three_steps(tmp_path):
    config_dir = tmp_path / "config"
    input_dir = tmp_path / "data" / "input"
    config_dir.mkdir()
    input_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        """
app:
  database_path: "data/test.db"
  output_dir: "data/output"
input_files:
  sap_monthly: "data/input/sap.csv"
  table2_material_master: "data/input/table2_material_master.csv"
  table3_model_coef: "data/input/table3_model_coef.csv"
  table4_unit_fee: "data/input/table4_unit_fee.csv"
  table5_pnl_delivery_rule: "data/input/table5_pnl_delivery_rule.csv"
run:
  yyyymm: "202502"
  factory_scope: []
""",
        encoding="utf-8",
    )
    app = create_app(config_dir / "config.yaml")
    response = app.test_client().post(
        "/merge/flows/create",
        data={
            "flow_key": "many_steps_flow",
            "name": "多步骤流程",
            "right_table": "table2_material_master",
            "step_name": "匹配第1步",
            "match_fields": ["material_code"],
            "output_fields": ["model"],
            "step_2_enabled": "Y",
            "step_2_name": "匹配第2步",
            "step_2_right_table": "table2_material_master",
            "step_2_match_fields": ["material_code"],
            "step_4_enabled": "Y",
            "step_4_name": "匹配第4步",
            "step_4_right_table": "table2_material_master",
            "step_4_match_fields": ["material_code"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    raw = yaml.safe_load((config_dir / "merge_flows.yaml").read_text(encoding="utf-8"))
    assert len(raw["flows"]["many_steps_flow"]["steps"]) == 3
    assert raw["flows"]["many_steps_flow"]["steps"][-1]["name"] == "匹配第4步"


def test_can_disable_and_copy_config_table_version(tmp_path):
    config_dir = tmp_path / "config"
    input_dir = tmp_path / "data" / "input"
    config_dir.mkdir()
    input_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        """
app:
  database_path: "data/test.db"
  output_dir: "data/output"
input_files:
  sap_monthly: "data/input/sap.csv"
  table2_material_master: "data/input/table2_material_master.csv"
  table3_model_coef: "data/input/table3_model_coef.csv"
  table4_unit_fee: "data/input/table4_unit_fee.csv"
  table5_pnl_delivery_rule: "data/input/table5_pnl_delivery_rule.csv"
run:
  yyyymm: "202502"
  factory_scope: []
""",
        encoding="utf-8",
    )
    pd.DataFrame([{
        "yyyymm": "202502",
        "factory_code": "F001",
        "material_code": "MAT001",
    }]).to_csv(input_dir / "table2_material_master.csv", index=False)

    app = create_app(config_dir / "config.yaml")
    client = app.test_client()
    client.get("/config-tables")

    response = client.post(
        "/rules/table2_material_master/versions/default/status",
        data={"status": "INACTIVE"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    response = client.post(
        "/rules/table2_material_master/versions/default/copy",
        data={
            "new_version_id": "202502_v2",
            "new_version_name": "202502 调整版",
            "new_version_description": "测试复制版本",
            "created_by": "tester",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    catalog = yaml.safe_load((config_dir / "config_table_versions.yaml").read_text(encoding="utf-8"))
    versions = catalog["config_table_versions"]["table2_material_master"]
    default_version = next(item for item in versions if item["version_id"] == "default")
    copied_version = next(item for item in versions if item["version_id"] == "202502_v2")
    assert default_version["status"] == "INACTIVE"
    assert copied_version["name"] == "202502 调整版"
    assert copied_version["created_by"] == "tester"
    assert (tmp_path / copied_version["file"]).exists()


def test_settings_page_redirects_to_period_management(tmp_path):
    config_dir = tmp_path / "config"
    input_dir = tmp_path / "data" / "input"
    config_dir.mkdir()
    input_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        """
app:
  database_path: "data/test.db"
  output_dir: "data/output"
input_files:
  sap_monthly: "data/input/sap.csv"
  table2_material_master: "data/input/table2_material_master.csv"
  table3_model_coef: "data/input/table3_model_coef.csv"
  table4_unit_fee: "data/input/table4_unit_fee.csv"
  table5_pnl_delivery_rule: "data/input/table5_pnl_delivery_rule.csv"
run:
  yyyymm: "202502"
  factory_scope: []
""",
        encoding="utf-8",
    )
    app = create_app(config_dir / "config.yaml")

    response = app.test_client().get("/settings?period=202502")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/periods")


def test_can_move_and_delete_rule_table_type(tmp_path):
    config_dir = tmp_path / "config"
    input_dir = tmp_path / "data" / "input"
    config_dir.mkdir()
    input_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        """
app:
  database_path: "data/test.db"
  output_dir: "data/output"
input_files:
  sap_monthly: "data/input/sap.csv"
  table2_material_master: "data/input/table2_material_master.csv"
  table3_model_coef: "data/input/table3_model_coef.csv"
  table4_unit_fee: "data/input/table4_unit_fee.csv"
  table5_pnl_delivery_rule: "data/input/table5_pnl_delivery_rule.csv"
run:
  yyyymm: "202502"
  factory_scope: []
""",
        encoding="utf-8",
    )
    app = create_app(config_dir / "config.yaml")
    client = app.test_client()

    client.post(
        "/rules/create",
        data={"table_key": "custom_map", "name": "自定义映射表", "description": "测试"},
    )
    move_response = client.post("/rules/custom_map/type/move/up", follow_redirects=False)
    delete_response = client.post("/rules/custom_map/type/delete", follow_redirects=False)

    assert move_response.status_code == 302
    assert delete_response.status_code == 302
    catalog = yaml.safe_load((config_dir / "rule_tables.yaml").read_text(encoding="utf-8"))
    assert "custom_map" in catalog["deleted_rule_tables"]


def test_can_add_and_delete_merge_flow_step(tmp_path):
    config_dir = tmp_path / "config"
    input_dir = tmp_path / "data" / "input"
    config_dir.mkdir()
    input_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        """
app:
  database_path: "data/test.db"
  output_dir: "data/output"
input_files:
  sap_monthly: "data/input/sap.csv"
  table2_material_master: "data/input/table2_material_master.csv"
  table3_model_coef: "data/input/table3_model_coef.csv"
  table4_unit_fee: "data/input/table4_unit_fee.csv"
  table5_pnl_delivery_rule: "data/input/table5_pnl_delivery_rule.csv"
run:
  yyyymm: "202502"
  factory_scope: []
""",
        encoding="utf-8",
    )
    app = create_app(config_dir / "config.yaml")
    client = app.test_client()
    client.post(
        "/merge/flows/create",
        data={
            "flow_key": "flow_steps",
            "name": "步骤测试流程",
            "right_table": "table2_material_master",
            "step_name": "匹配物料",
            "match_fields": ["material_code"],
            "output_fields": ["model"],
        },
    )

    add_response = client.post("/merge/flows/flow_steps/steps/add", follow_redirects=False)
    delete_response = client.post("/merge/flows/flow_steps/steps/2/delete", follow_redirects=False)

    assert add_response.status_code == 302
    assert delete_response.status_code == 302
    raw = yaml.safe_load((config_dir / "merge_flows.yaml").read_text(encoding="utf-8"))
    assert len(raw["flows"]["flow_steps"]["steps"]) == 1


def test_can_delete_default_merge_flow_and_show_empty_state(tmp_path):
    config_dir = tmp_path / "config"
    input_dir = tmp_path / "data" / "input"
    config_dir.mkdir()
    input_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        """
app:
  database_path: "data/test.db"
  output_dir: "data/output"
input_files:
  sap_monthly: "data/input/sap.csv"
  table2_material_master: "data/input/table2_material_master.csv"
  table3_model_coef: "data/input/table3_model_coef.csv"
  table4_unit_fee: "data/input/table4_unit_fee.csv"
  table5_pnl_delivery_rule: "data/input/table5_pnl_delivery_rule.csv"
run:
  yyyymm: "202502"
  factory_scope: []
""",
        encoding="utf-8",
    )
    (config_dir / "merge_flows.yaml").write_text(
        """
flows:
  sap_material_master_v1:
    name: "默认流程"
    steps:
      - name: "匹配物料"
        right_table: "table2_material_master"
        keys:
          - {left: "material_code", right: "material_code"}
        output_fields: ["model"]
""",
        encoding="utf-8",
    )
    app = create_app(config_dir / "config.yaml")
    client = app.test_client()

    response = client.post("/merge/flows/sap_material_master_v1/delete", follow_redirects=False)
    empty_page = client.get("/rules?tab=flows")

    assert response.status_code == 302
    raw = yaml.safe_load((config_dir / "merge_flows.yaml").read_text(encoding="utf-8"))
    assert raw["flows"] == {}
    assert "暂无计算流程配置".encode() in empty_page.data


def test_flow_fields_are_derived_from_config_table_columns_and_prior_outputs(tmp_path):
    config_dir = tmp_path / "config"
    input_dir = tmp_path / "data" / "input"
    config_dir.mkdir()
    input_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        """
app:
  database_path: "data/test.db"
  output_dir: "data/output"
input_files:
  sap_monthly: "data/input/sap.csv"
  table2_material_master: "data/input/table2_material_master.csv"
  table3_model_coef: "data/input/table3_model_coef.csv"
  table4_unit_fee: "data/input/table4_unit_fee.csv"
  table5_pnl_delivery_rule: "data/input/table5_pnl_delivery_rule.csv"
run:
  yyyymm: "202502"
  factory_scope: []
""",
        encoding="utf-8",
    )
    pd.DataFrame([{"物料编码": "MAT001", "机型": "M1", "自定义输出": "X"}]).to_csv(input_dir / "table2_material_master.csv", index=False)
    pd.DataFrame([{"机型": "M1", "标准工时系数": 1.2, "综合难度系数": 1.1}]).to_csv(input_dir / "table3_model_coef.csv", index=False)
    (config_dir / "merge_flows.yaml").write_text(
        """
flows:
  dynamic_fields:
    name: "动态字段测试"
    steps:
      - name: "匹配物料"
        right_table: "table2_material_master"
        keys:
          - {left: "material_code", right: "material_code"}
        output_fields: ["model", "自定义输出"]
      - name: "匹配系数"
        right_table: "table3_model_coef"
        keys:
          - {left: "model", right: "model"}
        output_fields: ["std_hour_coef"]
""",
        encoding="utf-8",
    )
    app = create_app(config_dir / "config.yaml")
    cfg = app.config["FACTORY_FEE_CONFIG"]

    flow = _merge_flow_config(cfg, "dynamic_fields")
    step1 = flow["steps"][0]
    step2 = flow["steps"][1]

    assert [item["field"] for item in step1["match_options"]] == ["material_code"]
    assert "model" in [item["field"] for item in step1["output_options"]]
    assert "material_code" not in [item["field"] for item in step1["output_options"]]
    assert "model" in [item["field"] for item in step2["match_options"]]

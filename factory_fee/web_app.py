from __future__ import annotations

from dataclasses import replace
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

import pandas as pd
import yaml
from flask import Flask, Response, flash, redirect, render_template_string, request, send_file, send_from_directory, url_for
from werkzeug.utils import secure_filename

from .config import AppConfig, load_config
from .db import connect, init_db
from .io_files import normalize_common, read_table
from .merge_flow import run_merge_flow, _default_calculated_columns
from .pipeline import run_pipeline
from .sap_import import import_sap_file, normalize_yyyymm


RULE_TABLES: dict[str, dict[str, str]] = {
    "table2_material_master": {"name": "表2 物料主数据", "description": "物料、机型、项目、工艺等基础归类规则"},
    "table3_model_coef": {"name": "表3 机型系数", "description": "标准工时系数、综合难度系数规则"},
    "table4_unit_fee": {"name": "表4 单台加工费", "description": "单台加工费、币种、汇率规则"},
    "table5_pnl_delivery_rule": {"name": "表5 损益交货量", "description": "损益交货量是否计入与折算系数规则"},
}

RULE_STANDARD_FIELDS = {
    "table2_material_master": [
        "yyyymm",
        "factory_code",
        "factory_name",
        "location_code",
        "movement_type",
        "username",
        "material_code",
        "material_desc",
        "brand",
        "model",
        "project_name",
        "factory_material_group",
        "model_category",
        "process_category",
        "standard_type",
        "data_source",
        "created_at",
        "created_by",
        "is_active",
        "priority",
    ],
    "table3_model_coef": [
        "yyyymm",
        "factory_code",
        "factory_name",
        "model",
        "factory_material_group",
        "process_category",
        "shipment_type",
        "std_hour_coef",
        "difficulty_coef",
        "output_qty",
        "created_at",
        "created_by",
        "is_active",
        "priority",
    ],
    "table4_unit_fee": [
        "yyyymm",
        "factory_code",
        "factory_name",
        "model",
        "factory_material_group",
        "process_category",
        "shipment_type",
        "standard_type",
        "unit_fee_local",
        "currency_code",
        "exchange_rate",
        "unit_fee_cny",
        "created_at",
        "created_by",
        "is_active",
        "priority",
    ],
    "table5_pnl_delivery_rule": [
        "yyyymm",
        "factory_code",
        "factory_name",
        "model_category",
        "process_category",
        "factory_material_group",
        "include_pnl_delivery",
        "pnl_delivery_coef",
        "created_at",
        "created_by",
        "is_active",
        "priority",
    ],
}

ALLOWED_UPLOAD_SUFFIXES = {".csv", ".xlsx", ".xls"}
MERGE_FLOW_KEY = "sap_material_master_v1"

MERGE_MATCH_FIELD_OPTIONS = [
    {"field": "yyyymm", "label": "年月"},
    {"field": "factory_code", "label": "工厂代码"},
    {"field": "location_code", "label": "库位"},
    {"field": "movement_type", "label": "移动类型"},
    {"field": "username", "label": "用户名"},
    {"field": "material_code", "label": "物料编码"},
    {"field": "brand", "label": "品牌"},
    {"field": "model", "label": "机型"},
    {"field": "factory_material_group", "label": "工厂物料组"},
    {"field": "model_category", "label": "机型类别"},
    {"field": "process_category", "label": "工艺分类"},
    {"field": "standard_type", "label": "制式"},
    {"field": "shipment_type", "label": "出货类型"},
]

SAP_PREVIEW_LABELS = {
    "raw_id": "行号",
    "yyyymm": "年月",
    "factory_code": "工厂代码",
    "factory_name": "工厂名称",
    "movement_type": "移动类型",
    "location_code": "库位",
    "username": "用户名",
    "order_no": "订单号",
    "legal_entity_code": "法人编码",
    "biz_date": "业务日期",
    "material_group": "物料组",
    "material_code": "物料编码",
    "material_desc": "物料描述",
    "delivery_qty_original": "交货数量原值",
    "delivery_qty": "交货数量",
    "source_table_name": "来源表",
    "source_file_name": "来源文件",
    "imported_at": "导入时间",
    "source_row_no": "源文件行号",
}

RULE_PREVIEW_LABELS = {
    "yyyymm": "年月",
    "factory_code": "工厂代码",
    "factory_name": "工厂名称",
    "location_code": "库位",
    "movement_type": "移动类型",
    "username": "用户名",
    "material_code": "物料编码",
    "material_desc": "物料描述",
    "brand": "品牌",
    "model": "机型",
    "project_name": "项目名称",
    "factory_material_group": "工厂物料组",
    "model_category": "机型类别",
    "process_category": "工艺分类",
    "standard_type": "制式",
    "shipment_type": "出货类型",
    "std_hour_coef": "标准工时系数",
    "difficulty_coef": "综合难度系数",
    "output_qty": "产量",
    "unit_fee_local": "单台加工费本币",
    "currency_code": "货币",
    "exchange_rate": "汇率",
    "unit_fee_cny": "单台加工费CNY",
    "include_pnl_delivery": "是否计入损益交货量",
    "pnl_delivery_coef": "损益交货量系数",
    "data_source": "数据来源",
    "created_at": "创建时间",
    "created_by": "创建人",
    "is_active": "是否启用",
    "priority": "优先级",
}

STATUS_LABELS = {
    "SUCCESS": "成功",
    "RUNNING": "运行中",
    "FAILED": "失败",
    "MATCHED": "已匹配",
    "NOT_FOUND": "未匹配",
    "DUPLICATED": "重复匹配",
}

PERIOD_STATUS_LABELS = {
    "DRAFT": "草稿",
    "COMPLETED": "已完成",
    "CLOSED": "已封存",
    "ARCHIVED": "已归档",
}


def create_app(config_path: str | Path) -> Flask:
    cfg = load_config(config_path)
    app = Flask(__name__)
    app.secret_key = os.getenv("FACTORY_FEE_SECRET_KEY", "factory-fee-local-console")
    app.config["FACTORY_FEE_CONFIG"] = cfg

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "app": "factory-fee-console"}

    @app.get("/")
    def index() -> str:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        view_cfg = replace(cfg, yyyymm=period_id)
        summary = _dashboard_summary(view_cfg)
        batches = _sap_batches(view_cfg, period_id=period_id)
        merge_flows = _merge_flows_summary(cfg)
        selected_flow_key = request.args.get("flow") or (merge_flows[0]["key"] if merge_flows else MERGE_FLOW_KEY)
        execution_config = _execution_config(view_cfg, selected_flow_key)
        execution_summary = _execution_summary(view_cfg, batches, selected_flow_key)
        merge_config = _merge_material_config(view_cfg)
        rule_tables = _rule_tables(cfg)
        return render_template_string(
            BASE_TEMPLATE,
            page="dashboard",
            cfg=view_cfg,
            summary=summary,
            rule_tables=rule_tables,
            batches=batches,
            merge_config=merge_config,
            merge_flows=merge_flows,
            selected_flow_key=selected_flow_key,
            execution_config=execution_config,
            execution_summary=execution_summary,
            periods=_periods(cfg),
            selected_period=period_id,
        )

    @app.get("/settings")
    def settings() -> Response:
        return redirect(url_for("index"))

    @app.post("/run")
    def run() -> Response:
        cfg = _cfg(app)
        yyyymm = normalize_yyyymm(request.form.get("period_id", "").strip() or request.form.get("yyyymm", "").strip() or cfg.yyyymm)
        import_batch_id = request.form.get("import_batch_id", "").strip()
        flow_key = _safe_table_key(request.form.get("flow_key", MERGE_FLOW_KEY))
        if not import_batch_id:
            flash("请选择 SAP 数据批次后再计算。", "error")
            return redirect(url_for("index", period=yyyymm))
        if _is_period_locked(cfg, yyyymm):
            flash("当前期间已封存或归档，不能执行计算。", "error")
            return redirect(url_for("index", period=yyyymm))
        config_versions = {
            key.replace("config_version_", "", 1): value
            for key, value in request.form.items()
            if key.startswith("config_version_") and value
        }
        run_cfg = replace(cfg, yyyymm=yyyymm)
        conn = connect(run_cfg.database_path)
        try:
            summary = run_merge_flow(
                conn,
                run_cfg,
                import_batch_id=import_batch_id,
                flow_key=flow_key,
                config_versions=config_versions,
            )
        finally:
            conn.close()
        flash(f"计算完成：{summary['merge_run_id']}，批次 {summary['import_batch_id']}，输出 {summary['merged_rows']} 行，异常 {summary['exception_rows']} 行。", "success")
        return redirect(url_for("results", period=yyyymm, merge_run_id=summary["merge_run_id"]))

    @app.post("/merge/material")
    def merge_material() -> Response:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        import_batch_id = request.form.get("import_batch_id", "").strip()
        if not import_batch_id:
            flash("请选择 SAP 数据批次 import_batch_id 后再合并表2。", "error")
            return redirect(url_for("index", period=period_id))
        run_cfg = replace(cfg, yyyymm=period_id)
        conn = connect(cfg.database_path)
        try:
            summary = run_merge_flow(conn, run_cfg, import_batch_id=import_batch_id, flow_key=MERGE_FLOW_KEY)
        finally:
            conn.close()
        flash(
            f"表2合并完成：{summary['merge_run_id']}，输出 {summary['merged_rows']} 行，异常 {summary['exception_rows']} 行。",
            "success",
        )
        return redirect(url_for("results", period=period_id, merge_run_id=summary["merge_run_id"]))

    @app.post("/merge/material/config")
    def update_merge_material_config() -> Response:
        cfg = _cfg(app)
        selected_fields = request.form.getlist("match_fields")
        rule_table = request.form.get("rule_table", "table2_material_master").strip()
        return_to = request.form.get("return_to", "rules")
        rule_tables = _rule_tables(cfg)
        if rule_table not in rule_tables:
            flash("请选择有效的匹配规则表。", "error")
            return redirect(url_for("rules"))
        valid_fields = set(RULE_PREVIEW_LABELS) | set(SAP_PREVIEW_LABELS)
        selected_fields = [field for field in selected_fields if field in valid_fields]
        if not selected_fields:
            flash("请至少选择一个匹配字段。", "error")
            return redirect(url_for("rules"))
        _save_merge_material_fields(cfg, selected_fields, rule_table)
        labels = [item["label"] for item in MERGE_MATCH_FIELD_OPTIONS if item["field"] in selected_fields]
        flash(f"匹配逻辑已保存：{rule_tables[rule_table]['name']}；字段：{'、'.join(labels)}。", "success")
        return redirect(url_for("index") if return_to == "dashboard" else url_for("rules"))

    @app.get("/sap")
    def sap_data() -> str:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        batches = _sap_batches(cfg, period_id=period_id)
        selected_batch = request.args.get("import_batch_id") or (batches[0]["import_batch_id"] if batches else "")
        preview = _sap_batch_preview(cfg, selected_batch, limit=20) if selected_batch else {"columns": [], "rows": [], "row_count": 0}
        return render_template_string(
            BASE_TEMPLATE,
            page="sap",
            cfg=replace(cfg, yyyymm=period_id),
            rule_tables=_rule_tables(cfg),
            batches=batches,
            selected_batch=selected_batch,
            preview=preview,
            periods=_periods(cfg),
            selected_period=period_id,
        )

    @app.post("/sap/import")
    def upload_sap() -> Response:
        cfg = _cfg(app)
        uploaded = request.files.get("file")
        if not uploaded or uploaded.filename == "":
            flash("请选择要上传的 SAP 流水文件。", "error")
            return redirect(url_for("sap_data"))
        filename = secure_filename(uploaded.filename)
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_SUFFIXES:
            flash("仅支持 CSV、XLSX、XLS 文件。", "error")
            return redirect(url_for("sap_data"))
        yyyymm = normalize_yyyymm(request.form.get("period_id", "").strip() or request.form.get("yyyymm", "").strip() or cfg.yyyymm)
        if _is_period_locked(cfg, yyyymm):
            flash("当前期间已封存或归档，不能继续导入 SAP。", "error")
            return redirect(url_for("sap_data", period=yyyymm))
        factory_scope = _split_scope(request.form.get("factory_scope", ""))
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / filename
            uploaded.save(tmp_path)
            conn = connect(cfg.database_path)
            try:
                summary = import_sap_file(conn, tmp_path, yyyymm, factory_scope, source_file_name=filename)
                _ensure_period(conn, yyyymm)
            except Exception as exc:
                flash(f"SAP 流水导入失败：{exc}", "error")
                return redirect(url_for("sap_data"))
            finally:
                conn.close()
        flash(f"SAP 流水导入完成：{summary['import_batch_id']}，导入 {summary['import_rows']} 行。", "success")
        return redirect(url_for("sap_data", period=yyyymm, import_batch_id=summary["import_batch_id"]))

    @app.post("/sap/batches/<import_batch_id>/delete")
    def delete_sap_batch(import_batch_id: str) -> Response:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        if _is_period_locked(cfg, period_id):
            flash("当前期间已封存或归档，不能删除 SAP 批次。", "error")
            return redirect(url_for("sap_data", period=period_id))
        conn = connect(cfg.database_path)
        try:
            _delete_sap_batch(conn, import_batch_id)
        finally:
            conn.close()
        flash("SAP 导入批次及相关结果已删除。", "success")
        return redirect(url_for("sap_data", period=period_id))

    @app.get("/sap/import")
    def sap_import_get() -> Response:
        return redirect(url_for("sap_data"))

    @app.get("/rules")
    def rules() -> str:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        _ensure_period_rule_files(cfg, period_id)
        rules_tab = request.args.get("tab", "tables")
        if rules_tab not in {"tables", "flows"}:
            rules_tab = "tables"
        show_new_flow = request.args.get("new_flow") == "1"
        rule_tables = _rule_tables(cfg)
        selected = request.args.get("table", "table2_material_master")
        if rule_tables and selected not in rule_tables:
            selected = next(iter(rule_tables))
        if not rule_tables:
            selected = ""
            versions = []
            selected_version = ""
            preview = {"exists": False, "path": "", "columns": [], "rows": [], "row_count": 0}
        else:
            versions = _config_table_versions(cfg, selected)
            selected_version = request.args.get("version") or (versions[0]["version_id"] if versions else "")
            preview_path = _config_version_file_path(cfg, selected, selected_version) if selected_version else _rule_file_path(cfg, selected)
            preview = _table_preview(preview_path, selected)
        merge_config = _merge_material_config(replace(cfg, yyyymm=period_id))
        merge_flows = _merge_flows_summary(cfg)
        selected_flow_key = request.args.get("flow") or (merge_flows[0]["key"] if merge_flows else "")
        flow_config = _merge_flow_config(cfg, selected_flow_key)
        execution_config = _execution_config(cfg, selected_flow_key)
        batches = _sap_batches(cfg)
        return render_template_string(
            BASE_TEMPLATE,
            page="rules",
            rules_tab=rules_tab,
            show_new_flow=show_new_flow,
            selected_flow_key=selected_flow_key,
            cfg=replace(cfg, yyyymm=period_id),
            rule_tables=rule_tables,
            selected=selected,
            preview=preview,
            config_versions=versions,
            selected_version=selected_version,
            merge_config=merge_config,
            merge_flows=merge_flows,
            flow_config=flow_config,
            execution_config=execution_config,
            batches=batches,
            periods=_periods(cfg),
            selected_period=period_id,
            period_locked=_is_period_locked(cfg, period_id),
        )

    @app.get("/config-tables")
    def config_tables() -> str:
        return rules()

    @app.get("/flows")
    def flows() -> str:
        return redirect(url_for("rules", tab="flows", period=_selected_period_id(_cfg(app)), flow=request.args.get("flow", "")))

    @app.post("/rules/create")
    def create_rule_table() -> Response:
        cfg = _cfg(app)
        table_key = _safe_table_key(request.form.get("table_key", ""))
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        if not table_key or not name:
            flash("请填写规则表编码和名称。", "error")
            return redirect(url_for("rules"))
        rule_tables = _rule_tables(cfg)
        if table_key in rule_tables:
            flash("规则表编码已存在。", "error")
            return redirect(url_for("rules", table=table_key))
        catalog = _load_rule_catalog(cfg)
        catalog.setdefault("rule_tables", {})[table_key] = {
            "name": name,
            "description": description or "自定义匹配规则表",
            "file": f"data/input/{table_key}.csv",
        }
        _save_rule_catalog(cfg, catalog)
        path = _rule_file_path(cfg, table_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            pd.DataFrame().to_csv(path, index=False, encoding="utf-8-sig")
        flash(f"规则表已新增：{name}。", "success")
        return redirect(url_for("rules", table=table_key))

    @app.post("/rules/<table_key>/type/delete")
    def delete_rule_table_type(table_key: str) -> Response:
        cfg = _cfg(app)
        table_key = _safe_table_key(table_key)
        tables = _rule_tables(cfg)
        if table_key not in tables:
            flash("配置表类型不存在。", "error")
            return redirect(url_for("rules", tab="tables"))
        catalog = _load_rule_catalog(cfg)
        catalog.setdefault("rule_tables", {}).pop(table_key, None)
        deleted = catalog.setdefault("deleted_rule_tables", [])
        if table_key not in deleted:
            deleted.append(table_key)
        order = [key for key in catalog.get("rule_table_order", []) if key != table_key]
        catalog["rule_table_order"] = order
        _save_rule_catalog(cfg, catalog)
        flash("配置表类型已删除。", "success")
        remaining = [key for key in _rule_tables(cfg).keys() if key != table_key]
        return redirect(url_for("rules", tab="tables", table=remaining[0] if remaining else "", period=_selected_period_id(cfg)))

    @app.post("/rules/<table_key>/type/move/<direction>")
    def move_rule_table_type(table_key: str, direction: str) -> Response:
        cfg = _cfg(app)
        table_key = _safe_table_key(table_key)
        catalog = _load_rule_catalog(cfg)
        keys = list(_rule_tables(cfg).keys())
        if table_key in keys:
            index = keys.index(table_key)
            target = index - 1 if direction == "up" else index + 1
            if 0 <= target < len(keys):
                keys[index], keys[target] = keys[target], keys[index]
                catalog["rule_table_order"] = keys
                _save_rule_catalog(cfg, catalog)
                flash("配置表类型顺序已更新。", "success")
        return redirect(url_for("rules", tab="tables", table=table_key, period=_selected_period_id(cfg)))

    @app.post("/merge/flows/create")
    def create_merge_flow() -> Response:
        cfg = _cfg(app)
        flow_key = _safe_table_key(request.form.get("flow_key", ""))
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        main_table = request.form.get("main_table", "raw_sap_monthly_data").strip() or "raw_sap_monthly_data"
        right_table = request.form.get("right_table", "table2_material_master").strip()
        step_name = request.form.get("step_name", "").strip() or "匹配物料主数据"
        selected_fields = request.form.getlist("match_fields")
        output_fields = request.form.getlist("output_fields")
        extra_steps: list[dict[str, Any]] = []
        rule_tables = _rule_tables(cfg)
        if not flow_key or not name:
            flash("请填写流程编码和流程名称。", "error")
            return redirect(url_for("rules", tab="flows", new_flow="1"))
        if right_table not in rule_tables:
            flash("请选择有效的匹配规则表。", "error")
            return redirect(url_for("rules", tab="flows", new_flow="1"))
        valid_fields = {item["field"] for item in _step_match_field_options(cfg, _sap_fact_fields(), right_table)}
        selected_fields = [field for field in selected_fields if field in valid_fields]
        if not selected_fields:
            flash("请至少选择一个字段映射。", "error")
            return redirect(url_for("rules", tab="flows", new_flow="1"))
        left_fields = _merge_unique_fields(_sap_fact_fields(), output_fields)
        extra_indexes = sorted({
            int(key.split("_")[1])
            for key in request.form
            if key.startswith("step_") and key.endswith("_enabled") and key.split("_")[1].isdigit()
        })
        for extra_index in extra_indexes:
            if request.form.get(f"step_{extra_index}_enabled") != "Y":
                continue
            extra_table = request.form.get(f"step_{extra_index}_right_table", "").strip()
            if extra_table not in rule_tables:
                continue
            extra_valid_fields = {item["field"] for item in _step_match_field_options(cfg, left_fields, extra_table)}
            extra_fields = [
                field for field in request.form.getlist(f"step_{extra_index}_match_fields")
                if field in extra_valid_fields
            ]
            extra_outputs = request.form.getlist(f"step_{extra_index}_output_fields")
            if not extra_fields and extra_valid_fields:
                extra_fields = [next(iter(extra_valid_fields))]
            extra_steps.append({
                "name": request.form.get(f"step_{extra_index}_name", f"匹配步骤{extra_index}").strip() or f"匹配步骤{extra_index}",
                "right_table": extra_table,
                "match_fields": extra_fields,
                "output_fields": extra_outputs,
            })
            left_fields = _merge_unique_fields(left_fields, extra_outputs)
        _save_new_merge_flow(cfg, flow_key, name, description, main_table, right_table, step_name, selected_fields, output_fields, extra_steps=extra_steps)
        flash(f"匹配流程已新增：{name}。", "success")
        return redirect(url_for("rules", tab="flows", flow=flow_key))

    @app.post("/merge/flows/<flow_key>/update")
    def update_merge_flow(flow_key: str) -> Response:
        cfg = _cfg(app)
        flow_key = _safe_table_key(flow_key)
        rule_tables = _rule_tables(cfg)
        step_indexes = sorted({int(value) for value in request.form.getlist("step_index") if str(value).isdigit()})
        steps = []
        left_fields = _sap_fact_fields()
        for step_index in step_indexes:
            rule_table = request.form.get(f"step_{step_index}_rule_table", "").strip()
            if rule_table not in rule_tables:
                continue
            match_options = _step_match_field_options(cfg, left_fields, rule_table)
            valid_fields = {item["field"] for item in match_options}
            selected_fields = [
                field for field in request.form.getlist(f"step_{step_index}_match_fields")
                if field in valid_fields
            ]
            if not selected_fields and match_options:
                selected_fields = [match_options[0]["field"]]
            output_options = _step_output_field_options(cfg, rule_table, selected_fields)
            valid_outputs = {item["field"] for item in output_options}
            output_fields = [field for field in request.form.getlist(f"step_{step_index}_output_fields") if field in valid_outputs]
            steps.append({
                "name": request.form.get(f"step_{step_index}_name", f"步骤{step_index}").strip() or f"步骤{step_index}",
                "right_table": rule_table,
                "match_fields": selected_fields,
                "output_fields": output_fields,
            })
            left_fields = _merge_unique_fields(left_fields, output_fields)
        if not steps:
            flash("请至少配置一个有效步骤和一个字段映射。", "error")
            return redirect(url_for("rules", tab="flows", flow=flow_key))
        calculated_columns = _calculated_columns_from_form(request.form)
        _update_merge_flow_config(cfg, flow_key, steps, calculated_columns)
        flash("匹配流程配置已保存。", "success")
        return redirect(url_for("rules", tab="flows", flow=flow_key))

    @app.post("/merge/flows/<flow_key>/steps/add")
    def add_merge_flow_step(flow_key: str) -> Response:
        cfg = _cfg(app)
        flow_key = _safe_table_key(flow_key)
        raw = _load_merge_config(cfg)
        flow = raw.setdefault("flows", {}).setdefault(flow_key, {})
        steps = flow.setdefault("steps", [])
        rule_tables = _rule_tables(cfg)
        default_table = "table2_material_master" if "table2_material_master" in rule_tables else next(iter(rule_tables), "")
        if not default_table:
            flash("请先新增配置表类型。", "error")
            return redirect(url_for("rules", tab="flows", flow=flow_key, period=_selected_period_id(cfg)))
        steps.append({
            "name": f"新增步骤{len(steps) + 1}",
            "left_table": "previous_result" if steps else "raw_sap_monthly_data",
            "right_table": default_table,
            "join_type": "left",
            "unmatched": "mark_exception",
            "keys": [{"left": "material_code", "right": "material_code"}],
            "output_fields": [],
        })
        _save_merge_config(cfg, raw)
        flash("步骤已新增，请在步骤详情中调整配置。", "success")
        return redirect(url_for("rules", tab="flows", flow=flow_key, period=_selected_period_id(cfg)))

    @app.post("/merge/flows/<flow_key>/steps/<int:step_index>/delete")
    def delete_merge_flow_step(flow_key: str, step_index: int) -> Response:
        cfg = _cfg(app)
        flow_key = _safe_table_key(flow_key)
        raw = _load_merge_config(cfg)
        flow = raw.setdefault("flows", {}).setdefault(flow_key, {})
        steps = list(flow.get("steps", []))
        if len(steps) <= 1:
            flash("至少保留一个步骤。", "error")
            return redirect(url_for("rules", tab="flows", flow=flow_key, period=_selected_period_id(cfg)))
        if 1 <= step_index <= len(steps):
            steps.pop(step_index - 1)
            flow["steps"] = steps
            _save_merge_config(cfg, raw)
            flash("步骤已删除。", "success")
        return redirect(url_for("rules", tab="flows", flow=flow_key, period=_selected_period_id(cfg)))

    @app.post("/merge/flows/<flow_key>/run")
    def run_configured_merge_flow(flow_key: str) -> Response:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        flow_key = _safe_table_key(flow_key)
        import_batch_id = request.form.get("import_batch_id", "").strip()
        mode = request.form.get("mode", "full")
        if not import_batch_id:
            flash("请选择 SAP 数据批次后再运行。", "error")
            return redirect(url_for("rules", tab="flows", flow=flow_key, period=period_id))
        if _is_period_locked(cfg, period_id):
            flash("当前期间已封存或归档，不能运行匹配流程。", "error")
            return redirect(url_for("rules", tab="flows", flow=flow_key, period=period_id))
        config_versions = {
            key.replace("config_version_", "", 1): value
            for key, value in request.form.items()
            if key.startswith("config_version_") and value
        }
        row_limit = 50 if mode == "test" else None
        run_prefix = "TEST" if mode == "test" else "MERGE"
        run_cfg = replace(cfg, yyyymm=period_id)
        conn = connect(cfg.database_path)
        try:
            summary = run_merge_flow(
                conn,
                run_cfg,
                import_batch_id=import_batch_id,
                flow_key=flow_key,
                merge_run_id=None if mode != "test" else f"{run_prefix}_{flow_key}_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}",
                row_limit=row_limit,
                config_versions=config_versions,
            )
        finally:
            conn.close()
        flash(f"匹配流程运行完成：{summary['merge_run_id']}，输出 {summary['merged_rows']} 行，异常 {summary['exception_rows']} 行。", "success")
        return redirect(url_for("results", period=period_id, merge_run_id=summary["merge_run_id"]))

    @app.post("/rules/<table_key>/upload")
    def upload_rule(table_key: str) -> Response:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        if _is_period_locked(cfg, period_id):
            flash("当前期间已封存或归档，不能更新规则表。", "error")
            return redirect(url_for("rules", tab="tables", table=table_key, period=period_id))
        rule_tables = _rule_tables(cfg)
        if table_key not in rule_tables:
            flash("未知规则表。", "error")
            return redirect(url_for("rules", period=period_id))
        uploaded = request.files.get("file")
        if not uploaded or uploaded.filename == "":
            flash("请选择要上传的规则文件。", "error")
            return redirect(url_for("rules", table=table_key, period=period_id))
        filename = secure_filename(uploaded.filename)
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_SUFFIXES:
            flash("仅支持 CSV、XLSX、XLS 文件。", "error")
            return redirect(url_for("rules", table=table_key, period=period_id))

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / filename
            uploaded.save(tmp_path)
            df = normalize_common(read_table(tmp_path))
        df = _order_rule_df_for_storage(df, table_key)
        version_id = request.form.get("version_id", "").strip() or pd.Timestamp.now().strftime("v_%Y%m%d_%H%M%S")
        version_name = request.form.get("version_name", "").strip() or f"{rule_tables[table_key]['name']} {version_id}"
        description = request.form.get("version_description", "").strip()
        created_by = request.form.get("created_by", "").strip() or "admin"
        _save_config_table_version(cfg, table_key, version_id, version_name, description, created_by, df)
        flash(f"{rule_tables[table_key]['name']} 版本已新增：{version_name}，共 {len(df)} 行。", "success")
        return redirect(url_for("rules", table=table_key, version=version_id, period=period_id))

    @app.post("/rules/<table_key>/versions/<version_id>/status")
    def update_config_version_status(table_key: str, version_id: str) -> Response:
        cfg = _cfg(app)
        status = request.form.get("status", "ACTIVE")
        if status not in {"ACTIVE", "INACTIVE"}:
            status = "ACTIVE"
        _set_config_table_version_status(cfg, table_key, version_id, status)
        flash("配置表版本状态已更新。", "success")
        return redirect(url_for("rules", tab="tables", table=table_key, version=version_id, period=_selected_period_id(cfg)))

    @app.post("/rules/<table_key>/versions/<version_id>/copy")
    def copy_config_version(table_key: str, version_id: str) -> Response:
        cfg = _cfg(app)
        new_version_id = request.form.get("new_version_id", "").strip()
        new_name = request.form.get("new_version_name", "").strip()
        description = request.form.get("new_version_description", "").strip()
        created_by = request.form.get("created_by", "").strip() or "admin"
        try:
            copied_id = _copy_config_table_version(cfg, table_key, version_id, new_version_id, new_name, description, created_by)
        except Exception as exc:
            flash(f"复制版本失败：{exc}", "error")
            return redirect(url_for("rules", tab="tables", table=table_key, version=version_id, period=_selected_period_id(cfg)))
        flash("配置表版本已复制。", "success")
        return redirect(url_for("rules", tab="tables", table=table_key, version=copied_id, period=_selected_period_id(cfg)))

    @app.post("/rules/<table_key>/delete")
    def delete_rule_table_data(table_key: str) -> Response:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        if _is_period_locked(cfg, period_id):
            flash("当前期间已封存或归档，不能删除规则表数据。", "error")
            return redirect(url_for("rules", tab="tables", table=table_key, period=period_id))
        version_id = request.form.get("version", "").strip()
        path = _config_version_file_path(cfg, table_key, version_id) if version_id else _rule_file_path(cfg, table_key, period_id=period_id)
        if path.exists():
            _backup_existing_file(path)
            path.unlink()
        if version_id:
            catalog = _load_config_version_catalog(cfg)
            versions = catalog.setdefault("config_table_versions", {}).setdefault(table_key, [])
            versions[:] = [item for item in versions if str(item.get("version_id")) != version_id]
            _save_config_version_catalog(cfg, catalog)
        flash("配置表版本已删除。", "success")
        return redirect(url_for("rules", tab="tables", table=table_key, period=period_id))

    @app.get("/rules/<table_key>/download")
    def download_rule(table_key: str) -> Response:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        rule_tables = _rule_tables(cfg)
        if table_key not in rule_tables:
            flash("未知规则表。", "error")
            return redirect(url_for("rules"))
        version_id = request.args.get("version", "")
        path = _config_version_file_path(cfg, table_key, version_id) if version_id else _rule_file_path(cfg, table_key, period_id=period_id)
        if not path.exists():
            flash("当前规则文件不存在。", "error")
            return redirect(url_for("rules", table=table_key, period=period_id))
        export_df = _rule_export_df(path, table_key)
        output = BytesIO()
        sheet_name = rule_tables[table_key]["name"][:31]
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            export_df.to_excel(writer, index=False, sheet_name=sheet_name)
        output.seek(0)
        return send_file(
            output,
            as_attachment=True,
            download_name=f"{table_key}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/runs")
    def runs() -> str:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        rows = _recent_runs(cfg, period_id=period_id)
        merge_rows = _merge_runs(cfg, period_id=period_id)
        return render_template_string(BASE_TEMPLATE, page="runs", cfg=replace(cfg, yyyymm=period_id), rule_tables=_rule_tables(cfg), runs=rows, merge_runs=merge_rows, periods=_periods(cfg), selected_period=period_id)

    @app.get("/help")
    def help_page() -> str:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        return render_template_string(
            BASE_TEMPLATE,
            page="help",
            cfg=replace(cfg, yyyymm=period_id),
            rule_tables=_rule_tables(cfg),
            periods=_periods(cfg),
            selected_period=period_id,
        )

    @app.get("/results")
    def results() -> str:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        merge_rows = _merge_runs(cfg, limit=100, period_id=period_id)
        selected_run_id = request.args.get("merge_run_id") or (merge_rows[0]["merge_run_id"] if merge_rows else "")
        detail = _merge_run_detail(cfg, selected_run_id) if selected_run_id else {
            "exists": False,
            "merge_run_id": "",
            "result_preview": {"columns": [], "rows": [], "row_count": 0},
            "exception_preview": {"columns": [], "rows": [], "row_count": 0},
        }
        return render_template_string(
            BASE_TEMPLATE,
            page="results",
            cfg=replace(cfg, yyyymm=period_id),
            rule_tables=_rule_tables(cfg),
            merge_runs=merge_rows,
            selected_run_id=selected_run_id,
            detail=detail,
            periods=_periods(cfg),
            selected_period=period_id,
        )

    @app.get("/exports")
    def exports() -> str:
        cfg = _cfg(app)
        export_context = _export_context(cfg)
        return render_template_string(
            BASE_TEMPLATE,
            page="exports",
            cfg=replace(cfg, yyyymm=cfg.yyyymm),
            rule_tables=_rule_tables(cfg),
            merge_runs=export_context["merge_runs"],
            selected_run_id=export_context["selected_run_id"],
            export_fields=export_context["field_options"],
            selected_export_fields=export_context["selected_fields"],
            export_preview=export_context["preview"],
            export_history=_export_history(cfg),
            selected_period=cfg.yyyymm,
        )

    @app.post("/exports/create")
    def create_export() -> Response:
        cfg = _cfg(app)
        merge_run_id = request.form.get("merge_run_id", "").strip()
        selected_fields = request.form.getlist("export_fields")
        if not merge_run_id:
            flash("请选择要导出的计算结果。", "error")
            return redirect(url_for("exports"))
        if not selected_fields:
            flash("请至少勾选一个导出字段。", "error")
            return redirect(url_for("exports", merge_run_id=merge_run_id))
        try:
            export_info = _save_processing_fee_export(cfg, merge_run_id, selected_fields)
        except Exception as exc:
            flash(f"导出失败：{exc}", "error")
            return redirect(url_for("exports", merge_run_id=merge_run_id))
        flash(f"加工费导出已生成：{export_info['file_name']}。", "success")
        return redirect(url_for("exports", merge_run_id=merge_run_id))

    @app.get("/exports/download/<period>/<path:filename>")
    def download_export(period: str, filename: str) -> Response:
        cfg = _cfg(app)
        safe_period = normalize_yyyymm(period)
        safe_name = Path(filename).name
        return send_from_directory(cfg.output_dir / "processing_fee_exports" / safe_period, safe_name, as_attachment=True)

    @app.post("/merge/flows/<flow_key>/delete")
    def delete_merge_flow(flow_key: str) -> Response:
        cfg = _cfg(app)
        flow_key = _safe_table_key(flow_key)
        raw = _load_merge_config(cfg)
        if raw.get("flows", {}).pop(flow_key, None) is not None:
            _save_merge_config(cfg, raw)
            flash("匹配流程已删除。", "success")
        else:
            flash("未找到要删除的匹配流程。", "error")
        period_id = _selected_period_id(cfg)
        remaining = list(_load_merge_config(cfg).get("flows", {}).keys())
        return redirect(url_for("rules", tab="flows", flow=remaining[0] if remaining else "", period=period_id))

    @app.post("/merge/runs/<merge_run_id>/delete")
    def delete_merge_run(merge_run_id: str) -> Response:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        if _is_period_locked(cfg, period_id):
            flash("当前期间已封存或归档，不能删除结果。", "error")
            return redirect(url_for("results", period=period_id))
        conn = connect(cfg.database_path)
        try:
            _delete_merge_run(conn, cfg, merge_run_id)
        finally:
            conn.close()
        flash("计算结果已删除。", "success")
        return redirect(url_for("results", period=period_id))

    @app.post("/periods/create")
    def create_period() -> Response:
        cfg = _cfg(app)
        period_id = normalize_yyyymm(request.form.get("period_id", "").strip())
        if not period_id:
            flash("请填写期间。", "error")
            return redirect(request.referrer or url_for("index"))
        conn = connect(cfg.database_path)
        try:
            _ensure_period(conn, period_id)
        finally:
            conn.close()
        _ensure_period_rule_files(cfg, period_id)
        flash(f"期间已创建：{period_id}。", "success")
        return redirect(url_for("settings", period=period_id))

    @app.post("/periods/<period_id>/<action>")
    def update_period_status(period_id: str, action: str) -> Response:
        cfg = _cfg(app)
        period_id = normalize_yyyymm(period_id)
        status_by_action = {"complete": "COMPLETED", "close": "CLOSED", "archive": "ARCHIVED", "reopen": "DRAFT"}
        status = status_by_action.get(action)
        if not status:
            flash("未知期间操作。", "error")
            return redirect(request.referrer or url_for("index", period=period_id))
        conn = connect(cfg.database_path)
        try:
            _ensure_period(conn, period_id)
            field = "closed_at" if status == "CLOSED" else "archived_at" if status == "ARCHIVED" else None
            if field:
                conn.execute(f"UPDATE period SET status=?, {field}=CURRENT_TIMESTAMP WHERE period_id=?", (status, period_id))
            else:
                conn.execute("UPDATE period SET status=? WHERE period_id=?", (status, period_id))
            conn.commit()
        finally:
            conn.close()
        flash(f"期间状态已更新：{PERIOD_STATUS_LABELS.get(status, status)}。", "success")
        return redirect(request.referrer or url_for("index", period=period_id))

    @app.get("/merge/runs/<merge_run_id>")
    def merge_run_detail(merge_run_id: str) -> str:
        cfg = _cfg(app)
        detail = _merge_run_detail(cfg, merge_run_id)
        return render_template_string(BASE_TEMPLATE, page="merge_detail", cfg=cfg, rule_tables=_rule_tables(cfg), detail=detail)

    @app.get("/outputs/<path:filename>")
    def output_file(filename: str) -> Response:
        cfg = _cfg(app)
        safe_name = Path(filename).name
        if safe_name.endswith(".xlsx"):
            localized = _localized_output_file(cfg.output_dir / safe_name)
            if localized is not None:
                return localized
        return send_from_directory(cfg.output_dir, safe_name, as_attachment=True)

    return app


def _cfg(app: Flask) -> AppConfig:
    return app.config["FACTORY_FEE_CONFIG"]


def _split_scope(value: str) -> list[str]:
    return [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]


def _rule_catalog_path(cfg: AppConfig) -> Path:
    return cfg.root_dir / "config" / "rule_tables.yaml"


def _load_rule_catalog(cfg: AppConfig) -> dict[str, Any]:
    path = _rule_catalog_path(cfg)
    if not path.exists():
        return {"rule_tables": {}}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"rule_tables": {}}


def _save_rule_catalog(cfg: AppConfig, catalog: dict[str, Any]) -> None:
    path = _rule_catalog_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(catalog, f, allow_unicode=True, sort_keys=False)


def _rule_tables(cfg: AppConfig) -> dict[str, dict[str, str]]:
    tables = {key: dict(value) for key, value in RULE_TABLES.items()}
    catalog = _load_rule_catalog(cfg)
    for key, meta in catalog.get("rule_tables", {}).items():
        tables[str(key)] = {
            "name": str(meta.get("name", key)),
            "description": str(meta.get("description", "自定义匹配规则表")),
            "file": str(meta.get("file", f"data/input/{key}.csv")),
        }
    for key in catalog.get("deleted_rule_tables", []):
        tables.pop(str(key), None)
    order = [str(key) for key in catalog.get("rule_table_order", []) if str(key) in tables]
    order.extend([key for key in tables.keys() if key not in order])
    return {key: tables[key] for key in order}


def _rule_file_path(cfg: AppConfig, table_key: str, period_id: str | None = None) -> Path:
    if period_id:
        period_path = _period_rule_file_path(cfg, table_key, period_id)
        if period_path.exists() or period_path.parent.exists():
            return period_path
    if table_key in cfg.input_files:
        return cfg.input_files[table_key]
    meta = _rule_tables(cfg).get(table_key, {})
    return (cfg.root_dir / meta.get("file", f"data/input/{table_key}.csv")).resolve()


def _config_version_catalog_path(cfg: AppConfig) -> Path:
    return cfg.root_dir / "config" / "config_table_versions.yaml"


def _load_config_version_catalog(cfg: AppConfig) -> dict[str, Any]:
    path = _config_version_catalog_path(cfg)
    if not path.exists():
        return {"config_table_versions": {}}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"config_table_versions": {}}


def _save_config_version_catalog(cfg: AppConfig, catalog: dict[str, Any]) -> None:
    path = _config_version_catalog_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(catalog, f, allow_unicode=True, sort_keys=False)


def _ensure_config_table_versions(cfg: AppConfig) -> None:
    catalog = _load_config_version_catalog(cfg)
    versions = catalog.setdefault("config_table_versions", {})
    changed = False
    for table_key, meta in _rule_tables(cfg).items():
        table_versions = versions.setdefault(table_key, [])
        if table_versions:
            continue
        source = _rule_file_path(cfg, table_key)
        if not source.exists():
            continue
        version_id = "default"
        target = cfg.root_dir / "data" / "config_tables" / table_key / f"{version_id}.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(source.read_bytes())
        table_versions.append({
            "version_id": version_id,
            "name": f"{meta['name']} 默认版",
            "description": "由现有配置表自动生成",
            "created_by": "system",
            "file": str(target.relative_to(cfg.root_dir)),
            "status": "ACTIVE",
            "created_at": pd.Timestamp.now().isoformat(),
        })
        changed = True
    if changed:
        _save_config_version_catalog(cfg, catalog)


def _config_table_versions(cfg: AppConfig, table_key: str) -> list[dict[str, Any]]:
    _ensure_config_table_versions(cfg)
    catalog = _load_config_version_catalog(cfg)
    out = []
    for version in catalog.get("config_table_versions", {}).get(table_key, []):
        row = dict(version)
        row["path"] = str((cfg.root_dir / str(row.get("file", ""))).resolve())
        row["status_label"] = "启用" if row.get("status") == "ACTIVE" else "停用"
        out.append(row)
    return out


def _config_version_name(cfg: AppConfig, table_key: str, version_id: str) -> str:
    for version in _config_table_versions(cfg, table_key):
        if str(version.get("version_id")) == str(version_id):
            return str(version.get("name", version_id))
    return version_id


def _config_versions_label(cfg: AppConfig, raw_json: str) -> str:
    try:
        mapping = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        mapping = {}
    labels = []
    for table_key, version_id in mapping.items():
        table_name = _rule_tables(cfg).get(table_key, {}).get("name", table_key)
        labels.append(f"{table_name}: {_config_version_name(cfg, table_key, str(version_id))}")
    return "；".join(labels)


def _config_version_file_path(cfg: AppConfig, table_key: str, version_id: str) -> Path:
    for version in _config_table_versions(cfg, table_key):
        if str(version.get("version_id")) == str(version_id):
            return Path(str(version["path"]))
    return (cfg.root_dir / "data" / "config_tables" / table_key / f"{version_id}.csv").resolve()


def _save_config_table_version(
    cfg: AppConfig,
    table_key: str,
    version_id: str,
    name: str,
    description: str,
    created_by: str,
    df: pd.DataFrame,
) -> None:
    version_id = _safe_table_key(version_id) or pd.Timestamp.now().strftime("v_%Y%m%d_%H%M%S")
    target = cfg.root_dir / "data" / "config_tables" / table_key / f"{version_id}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(target, index=False, encoding="utf-8-sig")
    catalog = _load_config_version_catalog(cfg)
    versions = catalog.setdefault("config_table_versions", {}).setdefault(table_key, [])
    versions[:] = [item for item in versions if str(item.get("version_id")) != version_id]
    versions.insert(0, {
        "version_id": version_id,
        "name": name or version_id,
        "description": description,
        "created_by": created_by or "admin",
        "file": str(target.relative_to(cfg.root_dir)),
        "status": "ACTIVE",
        "created_at": pd.Timestamp.now().isoformat(),
    })
    _save_config_version_catalog(cfg, catalog)


def _set_config_table_version_status(cfg: AppConfig, table_key: str, version_id: str, status: str) -> None:
    catalog = _load_config_version_catalog(cfg)
    versions = catalog.setdefault("config_table_versions", {}).setdefault(table_key, [])
    for version in versions:
        if str(version.get("version_id")) == str(version_id):
            version["status"] = status
            version["updated_at"] = pd.Timestamp.now().isoformat()
            break
    _save_config_version_catalog(cfg, catalog)


def _copy_config_table_version(
    cfg: AppConfig,
    table_key: str,
    source_version_id: str,
    new_version_id: str,
    new_name: str,
    description: str,
    created_by: str,
) -> str:
    source = _config_version_file_path(cfg, table_key, source_version_id)
    if not source.exists():
        raise ValueError("源版本文件不存在。")
    new_version_id = _safe_table_key(new_version_id) or pd.Timestamp.now().strftime("v_%Y%m%d_%H%M%S")
    target = cfg.root_dir / "data" / "config_tables" / table_key / f"{new_version_id}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    catalog = _load_config_version_catalog(cfg)
    versions = catalog.setdefault("config_table_versions", {}).setdefault(table_key, [])
    versions[:] = [item for item in versions if str(item.get("version_id")) != new_version_id]
    versions.insert(0, {
        "version_id": new_version_id,
        "name": new_name or f"{_config_version_name(cfg, table_key, source_version_id)} 副本",
        "description": description or f"复制自 {source_version_id}",
        "created_by": created_by or "admin",
        "file": str(target.relative_to(cfg.root_dir)),
        "status": "ACTIVE",
        "created_at": pd.Timestamp.now().isoformat(),
        "copied_from": source_version_id,
    })
    _save_config_version_catalog(cfg, catalog)
    return new_version_id


def _safe_table_key(value: str) -> str:
    value = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    return "".join(ch for ch in value if ch.isalnum() or ch == "_")


def _connect_for_read(cfg: AppConfig) -> sqlite3.Connection:
    conn = connect(cfg.database_path)
    init_db(conn)
    return conn


def _selected_period_id(cfg: AppConfig) -> str:
    return normalize_yyyymm(request.values.get("period", "").strip() or cfg.yyyymm)


def _periods(cfg: AppConfig) -> list[dict[str, Any]]:
    conn = _connect_for_read(cfg)
    try:
        _ensure_period(conn, cfg.yyyymm)
        for row in conn.execute("SELECT DISTINCT COALESCE(period_id, yyyymm) FROM sap_import_batch WHERE COALESCE(period_id, yyyymm) <> ''").fetchall():
            _ensure_period(conn, str(row[0]))
        rows = pd.read_sql_query(
            """
            SELECT period_id, name, status, created_at, closed_at, archived_at, message
            FROM period
            ORDER BY period_id DESC
            """,
            conn,
        ).to_dict("records")
    finally:
        conn.close()
    for row in rows:
        row["status_label"] = PERIOD_STATUS_LABELS.get(str(row.get("status")), row.get("status"))
    return rows


def _ensure_period(conn: sqlite3.Connection, period_id: str) -> None:
    init_db(conn)
    period_id = normalize_yyyymm(period_id)
    conn.execute(
        """
        INSERT OR IGNORE INTO period(period_id, name, status, message)
        VALUES (?, ?, ?, ?)
        """,
        (period_id, period_id, "DRAFT", "auto_created"),
    )
    conn.commit()


def _period_status(cfg: AppConfig, period_id: str) -> str:
    conn = _connect_for_read(cfg)
    try:
        _ensure_period(conn, period_id)
        row = conn.execute("SELECT status FROM period WHERE period_id=?", (period_id,)).fetchone()
        return str(row[0] if row else "DRAFT")
    finally:
        conn.close()


def _is_period_locked(cfg: AppConfig, period_id: str) -> bool:
    return False


def _ensure_period_rule_files(cfg: AppConfig, period_id: str) -> None:
    for table_key in _rule_tables(cfg):
        target = _period_rule_file_path(cfg, table_key, period_id)
        if target.exists():
            continue
        source = _rule_file_path(cfg, table_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            target.write_bytes(source.read_bytes())
        else:
            pd.DataFrame().to_csv(target, index=False, encoding="utf-8-sig")


def _period_rule_file_path(cfg: AppConfig, table_key: str, period_id: str) -> Path:
    return (cfg.root_dir / "data" / "periods" / normalize_yyyymm(period_id) / "input" / f"{table_key}.csv").resolve()


def _dashboard_summary(cfg: AppConfig) -> dict[str, Any]:
    conn = _connect_for_read(cfg)
    try:
        runs = pd.read_sql_query(
            """
            SELECT run_id, import_batch_id, sap_file_name, sap_import_rows, calc_result_rows, calc_exception_rows,
                   yyyymm, factory_scope, status, started_at, finished_at, message
            FROM calc_run_log
            WHERE COALESCE(period_id, yyyymm) = ?
            ORDER BY started_at DESC
            LIMIT 5
            """,
            conn,
            params=(cfg.yyyymm,),
        ).to_dict("records")
        counts = {
            "calc_result": _count(conn, "calc_result", period_id=cfg.yyyymm),
            "calc_exception": _count(conn, "calc_exception", period_id=cfg.yyyymm),
            "merge_result": _count(conn, "merge_result", period_id=cfg.yyyymm),
            "merge_exception": _count(conn, "merge_exception", period_id=cfg.yyyymm),
            "rule_snapshots": _count(conn, "rule_table_snapshot"),
            "sap_rows": _count(conn, "raw_sap_monthly_data", period_id=cfg.yyyymm),
            "sap_batches": _count(conn, "sap_import_batch", period_id=cfg.yyyymm),
        }
        merge_runs = _recent_merge_runs(conn, limit=5, period_id=cfg.yyyymm)
    finally:
        conn.close()
    return {"runs": runs, "merge_runs": merge_runs, "counts": counts}


def _recent_merge_runs(conn: sqlite3.Connection, limit: int = 10, period_id: str | None = None) -> list[dict[str, Any]]:
    where = "WHERE COALESCE(period_id, '') = ?" if period_id else ""
    params: tuple[Any, ...] = (period_id, limit) if period_id else (limit,)
    rows = pd.read_sql_query(
        f"""
        SELECT merge_run_id, flow_name, import_batch_id, config_versions_json, left_rows, merged_rows, exception_rows,
               status, started_at, finished_at, result_file, exception_file
        FROM merge_run_log
        {where}
        ORDER BY started_at DESC
        LIMIT ?
        """,
        conn,
        params=params,
    ).to_dict("records")
    return [_localize_status_fields(row) for row in rows]


def _recent_runs(cfg: AppConfig, period_id: str | None = None) -> list[dict[str, Any]]:
    conn = _connect_for_read(cfg)
    try:
        where = "WHERE COALESCE(period_id, yyyymm) = ?" if period_id else ""
        params: tuple[Any, ...] = (period_id,) if period_id else ()
        rows = pd.read_sql_query(
            f"""
            SELECT run_id, import_batch_id, sap_file_name, sap_import_rows, calc_result_rows, calc_exception_rows,
                   yyyymm, factory_scope, status, started_at, finished_at, message
            FROM calc_run_log
            {where}
            ORDER BY started_at DESC
            LIMIT 50
            """,
            conn,
            params=params,
        ).to_dict("records")
    finally:
        conn.close()
    output_files = {p.name for p in cfg.output_dir.glob("*.xlsx")}
    for row in rows:
        run_id = row["run_id"]
        row["result_file"] = f"calc_result_{run_id}.xlsx" if f"calc_result_{run_id}.xlsx" in output_files else ""
        row["exception_file"] = f"calc_exception_{run_id}.xlsx" if f"calc_exception_{run_id}.xlsx" in output_files else ""
        _localize_status_fields(row)
    return rows


def _merge_runs(cfg: AppConfig, limit: int = 50, period_id: str | None = None) -> list[dict[str, Any]]:
    conn = _connect_for_read(cfg)
    try:
        rows = _recent_merge_runs(conn, limit=limit, period_id=period_id)
    finally:
        conn.close()
    for row in rows:
        row["result_file_name"] = Path(str(row.get("result_file") or "")).name
        row["exception_file_name"] = Path(str(row.get("exception_file") or "")).name
    return rows


def _merge_run_detail(cfg: AppConfig, merge_run_id: str) -> dict[str, Any]:
    conn = _connect_for_read(cfg)
    try:
        run = conn.execute(
            """
            SELECT merge_run_id, flow_name, import_batch_id, config_versions_json, left_rows, merged_rows, exception_rows,
                   status, started_at, finished_at, result_file, exception_file, message
            FROM merge_run_log
            WHERE merge_run_id = ?
            """,
            (merge_run_id,),
        ).fetchone()
        if not run:
            return {
                "exists": False,
                "merge_run_id": merge_run_id,
                "result_preview": {"columns": [], "rows": [], "row_count": 0},
                "exception_preview": {"columns": [], "rows": [], "row_count": 0},
            }
        run_columns = [
            "merge_run_id", "flow_name", "import_batch_id", "config_versions_json", "left_rows", "merged_rows",
            "exception_rows", "status", "started_at", "finished_at", "result_file",
            "exception_file", "message",
        ]
        run_info = dict(zip(run_columns, run))
        run_info["config_versions_label"] = _config_versions_label(cfg, str(run_info.get("config_versions_json") or "{}"))
        _localize_status_fields(run_info)
        run_info["result_file_name"] = Path(str(run_info.get("result_file") or "")).name
        run_info["exception_file_name"] = Path(str(run_info.get("exception_file") or "")).name
        result_preview = _merge_json_preview(conn, "merge_result", merge_run_id, limit=50)
        exception_preview = _merge_exception_preview(conn, merge_run_id, limit=50)
        return {
            "exists": True,
            "run": run_info,
            "result_preview": result_preview,
            "exception_preview": exception_preview,
        }
    finally:
        conn.close()


def _merge_json_preview(conn: sqlite3.Connection, table_name: str, merge_run_id: str, limit: int = 50) -> dict[str, Any]:
    row_count = int(conn.execute(
        f"SELECT COUNT(*) FROM {table_name} WHERE merge_run_id = ?",
        (merge_run_id,),
    ).fetchone()[0])
    rows = conn.execute(
        f"SELECT raw_json FROM {table_name} WHERE merge_run_id = ? ORDER BY raw_id LIMIT ?",
        (merge_run_id, limit),
    ).fetchall()
    decoded = [_decode_raw_row(row[0]) for row in rows]
    return _records_preview(decoded, row_count)


def _merge_exception_preview(conn: sqlite3.Connection, merge_run_id: str, limit: int = 50) -> dict[str, Any]:
    row_count = int(conn.execute(
        "SELECT COUNT(*) FROM merge_exception WHERE merge_run_id = ?",
        (merge_run_id,),
    ).fetchone()[0])
    rows = conn.execute(
        """
        SELECT raw_id, exception_code, exception_message, hit_priority, hit_rule_id, raw_json
        FROM merge_exception
        WHERE merge_run_id = ?
        ORDER BY raw_id
        LIMIT ?
        """,
        (merge_run_id, limit),
    ).fetchall()
    records = []
    for raw_id, code, message, priority, rule_id, raw_json in rows:
        raw = _decode_raw_row(raw_json)
        records.append({
            "raw_id": raw_id,
            "exception_code": code,
            "exception_message": message,
            "hit_priority": priority,
            "hit_rule_id": rule_id,
            "material_code": raw.get("material_code", ""),
            "factory_code": raw.get("factory_code", ""),
            "yyyymm": raw.get("yyyymm", ""),
        })
    return _records_preview(records, row_count)


def _export_context(cfg: AppConfig) -> dict[str, Any]:
    conn = _connect_for_read(cfg)
    try:
        merge_runs = _recent_merge_runs(conn, limit=100)
        selected_run_id = request.values.get("merge_run_id", "").strip() or (merge_runs[0]["merge_run_id"] if merge_runs else "")
        records = _merge_result_records(conn, selected_run_id) if selected_run_id else []
    finally:
        conn.close()
    field_options = _export_field_options(records)
    requested_fields = request.values.getlist("export_fields")
    selection_submitted = request.values.get("field_selection") == "1"
    available = {item["field"] for item in field_options}
    selected_fields = [field for field in requested_fields if field in available]
    if not selected_fields and not selection_submitted:
        preferred = _default_export_fields()
        selected_fields = [field for field in preferred if field in available]
        if not selected_fields:
            selected_fields = [item["field"] for item in field_options[:12]]
    preview = _export_preview(records, selected_fields, limit=50)
    return {
        "merge_runs": merge_runs,
        "selected_run_id": selected_run_id,
        "field_options": field_options,
        "selected_fields": selected_fields,
        "preview": preview,
    }


def _merge_result_records(conn: sqlite3.Connection, merge_run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT raw_json FROM merge_result WHERE merge_run_id = ? ORDER BY raw_id",
        (merge_run_id,),
    ).fetchall()
    return [_decode_raw_row(row[0]) for row in rows]


def _export_field_options(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    fields: list[str] = []
    technical_prefixes = ("step",)
    technical_fields = {
        "merge_run_id",
        "raw_json",
        "exception_reason",
        "merge_exception_reason",
        "hit_rule_id",
        "hit_priority",
        "config_versions_json",
    }
    for record in records:
        for field in record:
            if field in technical_fields or any(str(field).startswith(prefix) for prefix in technical_prefixes):
                continue
            if field not in fields:
                fields.append(str(field))
    preferred = _default_export_fields()
    ordered = [field for field in preferred if field in fields]
    ordered.extend([field for field in fields if field not in ordered])
    return [{"field": field, "label": _display_column_label(field)} for field in ordered]


def _default_export_fields() -> list[str]:
    return [
        "yyyymm",
        "factory_code",
        "factory_name",
        "material_code",
        "material_desc",
        "brand",
        "model",
        "project_name",
        "factory_material_group",
        "model_category",
        "process_category",
        "standard_type",
        "delivery_qty",
        "pnl_delivery_qty",
        "unit_fee_cny",
        "fee_amount_cny",
        "std_hour_coef",
        "difficulty_coef",
        "calc_status",
    ]


def _export_preview(records: list[dict[str, Any]], fields: list[str], limit: int = 50) -> dict[str, Any]:
    selected_records = [
        {field: record.get(field, "") for field in fields}
        for record in records[:limit]
    ]
    return _records_preview(selected_records, len(records))


def _save_processing_fee_export(cfg: AppConfig, merge_run_id: str, fields: list[str]) -> dict[str, str]:
    conn = _connect_for_read(cfg)
    try:
        run_row = conn.execute(
            "SELECT flow_name, import_batch_id, period_id, started_at FROM merge_run_log WHERE merge_run_id = ?",
            (merge_run_id,),
        ).fetchone()
        records = _merge_result_records(conn, merge_run_id)
    finally:
        conn.close()
    if not records:
        raise ValueError("当前结果没有可导出的明细。")
    period = str((records[0].get("yyyymm") or (run_row[2] if run_row else "") or cfg.yyyymm)).replace("-", "")[:6]
    export_dir = cfg.output_dir / "processing_fee_exports" / period
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"processing_fee_export_{period}_{stamp}.xlsx"
    file_path = export_dir / file_name
    rows = [
        {_display_column_label(field): _display_cell_value(field, record.get(field, "")) for field in fields}
        for record in records
    ]
    pd.DataFrame(rows).to_excel(file_path, index=False)
    info = {
        "export_id": f"EXPORT_{period}_{stamp}",
        "period": period,
        "merge_run_id": merge_run_id,
        "flow_name": str(run_row[0] if run_row else ""),
        "import_batch_id": str(run_row[1] if run_row else ""),
        "file_name": file_name,
        "file_path": str(file_path.relative_to(cfg.root_dir)),
        "row_count": str(len(rows)),
        "field_count": str(len(fields)),
        "fields": list(fields),
        "created_at": pd.Timestamp.now().isoformat(),
    }
    history = _export_history_raw(cfg)
    history.setdefault("exports", []).insert(0, info)
    _save_export_history(cfg, history)
    return info


def _export_history_path(cfg: AppConfig) -> Path:
    return cfg.root_dir / "config" / "processing_fee_exports.yaml"


def _export_history_raw(cfg: AppConfig) -> dict[str, Any]:
    path = _export_history_path(cfg)
    if not path.exists():
        return {"exports": []}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"exports": []}


def _save_export_history(cfg: AppConfig, raw: dict[str, Any]) -> None:
    path = _export_history_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)


def _export_history(cfg: AppConfig) -> list[dict[str, Any]]:
    rows = []
    for item in _export_history_raw(cfg).get("exports", []):
        row = dict(item)
        row["download_name"] = Path(str(row.get("file_path", ""))).name
        row["file_name"] = row.get("file_name") or row["download_name"]
        rows.append(row)
    return rows


def _records_preview(records: list[dict[str, Any]], row_count: int) -> dict[str, Any]:
    columns: list[str] = []
    for record in records:
        for col in record.keys():
            if col not in columns:
                columns.append(col)
    display_columns = [_display_column_label(col) for col in columns]
    display_rows = []
    for record in records:
        display_rows.append({
            _display_column_label(col): _display_cell_value(col, record.get(col, ""))
            for col in columns
        })
    return {"columns": display_columns, "rows": display_rows, "row_count": row_count}


def _display_column_label(column: object) -> str:
    col = str(column)
    step_label = _step_column_label(col)
    if step_label:
        return step_label
    extra = {
        "merge_run_id": "合并运行ID",
        "flow_key": "流程编码",
        "match_status": "匹配状态",
        "hit_priority_material": "命中优先级",
        "hit_rule_id_material": "命中规则行",
        "merge_exception_reason": "异常原因",
        "exception_code": "异常编码",
        "exception_message": "异常说明",
        "hit_priority": "命中优先级",
        "hit_rule_id": "命中规则行",
        "run_id": "计算运行ID",
        "import_batch_id": "SAP导入批次",
        "source_file_name": "来源文件",
        "factory_scope": "工厂范围",
        "import_rows": "导入行数",
        "status": "状态",
        "started_at": "开始时间",
        "finished_at": "完成时间",
        "message": "说明",
        "flow_name": "流程名称",
        "config_versions_json": "配置表版本",
        "config_versions_label": "配置表版本",
        "left_rows": "输入行数",
        "merged_rows": "输出行数",
        "exception_rows": "异常行数",
        "result_file": "结果文件",
        "exception_file": "异常文件",
        "sap_file_name": "SAP文件",
        "sap_import_rows": "SAP导入行数",
        "calc_result_rows": "计算结果行数",
        "calc_exception_rows": "计算异常行数",
        "success_rows": "成功行数",
        "pnl_delivery_qty": "损益交货量",
        "unit_fee_cny": "单台加工费CNY",
        "fee_amount_cny": "加工费CNY",
        "std_hour": "标准工时",
        "difficulty_value": "综合难度",
        "calc_status": "计算状态",
        "exception_reason": "异常原因",
        "exception_stage": "异常阶段",
        "related_rule_id": "相关规则行",
        "related_priority": "相关优先级",
        "created_at": "创建时间",
    }
    return extra.get(col) or _known_column_label(col) or col


def _known_column_label(column: object) -> str | None:
    normalized = _normalize_header(column)
    for labels in (SAP_PREVIEW_LABELS, RULE_PREVIEW_LABELS):
        for key, label in labels.items():
            if normalized in {_normalize_header(key), _normalize_header(label)}:
                return label
    return None


def _step_column_label(column: str) -> str | None:
    if not column.startswith("step"):
        return None
    parts = column.split("_", 1)
    if len(parts) != 2 or not parts[0][4:].isdigit():
        return None
    step_no = parts[0][4:]
    suffix_labels = {
        "name": "名称",
        "match_status": "匹配状态",
        "hit_priority": "命中优先级",
        "hit_rule_id": "命中规则行",
    }
    suffix = suffix_labels.get(parts[1])
    return f"步骤{step_no}{suffix}" if suffix else None


def _display_cell_value(column: object, value: Any) -> Any:
    if value is None:
        return ""
    col = str(column)
    if col in {"status", "match_status", "calc_status"} or col.endswith("_match_status"):
        return STATUS_LABELS.get(str(value), value)
    return value


def _localize_status_fields(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("status", "match_status", "calc_status"):
        if key in row:
            row[f"{key}_label"] = STATUS_LABELS.get(str(row.get(key)), row.get(key))
    return row


def _localized_output_file(path: Path) -> Response | None:
    if not path.exists():
        return None
    try:
        sheets = pd.read_excel(path, sheet_name=None)
    except Exception:
        return None
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            localized = df.rename(columns={col: _display_column_label(col) for col in df.columns})
            localized.to_excel(writer, index=False, sheet_name=str(sheet_name)[:31])
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=path.name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _sap_batches(cfg: AppConfig, period_id: str | None = None) -> list[dict[str, Any]]:
    conn = _connect_for_read(cfg)
    try:
        where = "WHERE COALESCE(period_id, yyyymm) = ?" if period_id else ""
        params: tuple[Any, ...] = (period_id,) if period_id else ()
        rows = pd.read_sql_query(
            f"""
            SELECT import_batch_id, yyyymm, factory_scope, source_file_name, import_rows, imported_at, status, message
            FROM sap_import_batch
            {where}
            ORDER BY imported_at DESC
            LIMIT 100
            """,
            conn,
            params=params,
        ).to_dict("records")
        return [_localize_status_fields(row) for row in rows]
    finally:
        conn.close()


def _sap_batch_preview(cfg: AppConfig, import_batch_id: str, limit: int = 20) -> dict[str, Any]:
    if not import_batch_id:
        return {"columns": [], "rows": [], "row_count": 0}
    conn = _connect_for_read(cfg)
    try:
        row_count = int(conn.execute(
            "SELECT COUNT(*) FROM raw_sap_monthly_data WHERE import_batch_id = ?",
            (import_batch_id,),
        ).fetchone()[0])
        raw_rows = conn.execute(
            """
            SELECT source_columns_json, raw_json
            FROM raw_sap_monthly_data
            WHERE import_batch_id = ?
              AND raw_json IS NOT NULL
              AND raw_json <> ''
            ORDER BY raw_id
            LIMIT ?
            """,
            (import_batch_id, limit),
        ).fetchall()
        if raw_rows:
            columns = _decode_source_columns(raw_rows[0][0])
            rows = []
            for _, raw_json in raw_rows:
                raw = _decode_raw_row(raw_json)
                rows.append({_display_column_label(col): raw.get(col, "") for col in columns})
            return {"columns": [_display_column_label(col) for col in columns], "rows": rows, "row_count": row_count}

        df = pd.read_sql_query(
            """
            SELECT raw_id, yyyymm, factory_code, factory_name, movement_type, location_code, username,
                   order_no, legal_entity_code, biz_date, material_group, material_code, material_desc,
                   delivery_qty_original, delivery_qty, source_table_name, source_file_name, source_row_no
            FROM raw_sap_monthly_data
            WHERE import_batch_id = ?
            ORDER BY raw_id
            LIMIT ?
            """,
            conn,
            params=(import_batch_id, limit),
        )
    finally:
        conn.close()
    df = df.rename(columns={col: SAP_PREVIEW_LABELS.get(col, col) for col in df.columns})
    return {"columns": list(df.columns), "rows": df.to_dict("records"), "row_count": row_count}


def _decode_source_columns(value: str | None) -> list[str]:
    try:
        columns = json.loads(value or "[]")
    except json.JSONDecodeError:
        columns = []
    return [str(col) for col in columns]


def _decode_raw_row(value: str | None) -> dict[str, Any]:
    try:
        raw = json.loads(value or "{}")
    except json.JSONDecodeError:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def _count(conn: sqlite3.Connection, table_name: str, period_id: str | None = None) -> int:
    if period_id:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
        if "period_id" in columns:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table_name} WHERE period_id=?", (period_id,)).fetchone()[0])
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def _table_preview(path: Path, table_key: str | None = None, limit: int = 30) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path), "columns": [], "rows": [], "row_count": 0}
    try:
        df = _rule_export_df(path, table_key) if table_key else normalize_common(read_table(path))
    except Exception:
        return {"exists": True, "path": str(path), "columns": [], "rows": [], "row_count": 0}
    preview_df = df.head(limit)
    return {
        "exists": True,
        "path": str(path),
        "columns": list(preview_df.columns),
        "rows": preview_df.to_dict("records"),
        "row_count": len(df),
    }

def _rule_export_df(path: Path, table_key: str | None) -> pd.DataFrame:
    try:
        df = normalize_common(read_table(path))
    except Exception:
        return pd.DataFrame()
    if not table_key:
        return df
    ordered_columns = _ordered_rule_columns(df, table_key)
    out = df[ordered_columns].copy()
    rename_map = {}
    for col in ordered_columns:
        canonical = _canonical_rule_field(col)
        if canonical in RULE_PREVIEW_LABELS:
            rename_map[col] = RULE_PREVIEW_LABELS.get(canonical, col)
    return out.rename(columns=rename_map)


def _order_rule_df_for_storage(df: pd.DataFrame, table_key: str) -> pd.DataFrame:
    return df[_ordered_rule_columns(df, table_key)].copy()


def _ordered_rule_columns(df: pd.DataFrame, table_key: str) -> list[str]:
    standard_fields = RULE_STANDARD_FIELDS.get(table_key, [])
    canonical_to_source: dict[str, str] = {}
    used: set[str] = set()
    for col in df.columns:
        canonical = _canonical_rule_field(col)
        if canonical in standard_fields and canonical not in canonical_to_source:
            canonical_to_source[canonical] = col
    ordered = []
    for field in standard_fields:
        source = canonical_to_source.get(field)
        if source:
            ordered.append(source)
            used.add(source)
    ordered.extend([col for col in df.columns if col not in used])
    return ordered


def _canonical_rule_field(column: object) -> str:
    normalized = _normalize_header(column)
    for canonical, label in RULE_PREVIEW_LABELS.items():
        if normalized in {_normalize_header(canonical), _normalize_header(label)}:
            return canonical
    return str(column)


def _normalize_header(value: object) -> str:
    return str(value).strip().lower().replace(" ", "").replace("_", "")


def _merge_config_path(cfg: AppConfig) -> Path:
    return cfg.root_dir / "config" / "merge_flows.yaml"


def _load_merge_config(cfg: AppConfig) -> dict[str, Any]:
    path = _merge_config_path(cfg)
    if not path.exists():
        return {"flows": {}}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"flows": {}}


def _save_merge_config(cfg: AppConfig, raw: dict[str, Any]) -> None:
    path = _merge_config_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)


def _merge_material_config(cfg: AppConfig) -> dict[str, Any]:
    return _merge_flow_config(cfg, MERGE_FLOW_KEY)


def _merge_flow_config(cfg: AppConfig, flow_key: str) -> dict[str, Any]:
    raw = _load_merge_config(cfg)
    flow = raw.get("flows", {}).get(flow_key, {})
    rule_table_options = [
        {"key": key, "name": meta["name"]}
        for key, meta in _rule_tables(cfg).items()
    ]
    steps = _flow_config_steps(cfg, flow)
    selected_rule_table = steps[0]["right_table"] if steps else "table2_material_master"
    selected_fields = steps[0]["selected_fields"] if steps else []
    valid_fields = [item["field"] for item in (steps[0]["match_options"] if steps else [])]
    selected_fields = [field for field in selected_fields if field in valid_fields]
    selected_labels = [
        item["label"] for item in (steps[0]["match_options"] if steps else [])
        if item["field"] in selected_fields
    ]
    return {
        "flow_key": flow_key,
        "name": str(flow.get("name", flow_key)),
        "description": str(flow.get("description", "")),
        "field_options": steps[0]["match_options"] if steps else [],
        "selected_fields": selected_fields,
        "selected_labels": selected_labels,
        "selected_rule_table": selected_rule_table,
        "rule_table_options": rule_table_options,
        "steps": steps,
        "calculated_columns": _flow_calculated_columns_config(flow),
        "output_options": steps[0]["output_options"] if steps else [],
        "selected_output_fields": steps[0]["selected_output_fields"] if steps else [],
        "config_path": str(_merge_config_path(cfg)),
    }


def _execution_config(cfg: AppConfig, flow_key: str) -> dict[str, Any]:
    flow_config = _merge_flow_config(cfg, flow_key)
    steps = []
    for step in flow_config["steps"]:
        versions = _config_table_versions(cfg, step["right_table"])
        steps.append({
            **step,
            "rule_table_name": _rule_tables(cfg).get(step["right_table"], {}).get("name", step["right_table"]),
            "versions": versions,
            "selected_version": versions[0]["version_id"] if versions else "",
        })
    return {**flow_config, "steps": steps}


def _execution_summary(cfg: AppConfig, batches: list[dict[str, Any]], flow_key: str) -> dict[str, Any]:
    flow = _execution_config(cfg, flow_key)
    return {
        "period_id": cfg.yyyymm,
        "default_batch": batches[0] if batches else None,
        "flow": flow,
        "steps": flow["steps"],
    }


def _flow_config_steps(cfg: AppConfig, flow: dict[str, Any]) -> list[dict[str, Any]]:
    raw_steps = [dict(step) for step in flow.get("steps", []) if step.get("right_table")]
    if not raw_steps:
        priority_specs = flow.get("priority_specs", [])
        keys = priority_specs[0].get("keys", []) if priority_specs else []
        raw_steps = [{
            "name": "匹配规则表",
            "right_table": flow.get("right_table", "table2_material_master"),
            "keys": keys,
            "output_fields": flow.get("output_fields", []),
            "unmatched": flow.get("unmatched", "mark_exception"),
        }]
    steps = []
    rule_tables = _rule_tables(cfg)
    left_fields = _sap_fact_fields()
    for index, step in enumerate(raw_steps, start=1):
        right_table = str(step.get("right_table", "table2_material_master"))
        match_options = _step_match_field_options(cfg, left_fields, right_table)
        match_labels = {item["field"]: item["label"] for item in match_options}
        valid_match_fields = set(match_labels)
        selected_fields = [
            str(item.get("left", ""))
            for item in step.get("keys", [])
            if item.get("left") and str(item.get("left", "")) in valid_match_fields
        ]
        if not selected_fields and match_options:
            selected_fields = [match_options[0]["field"]]
        output_options = _step_output_field_options(cfg, right_table, selected_fields)
        output_labels = {item["field"]: item["label"] for item in output_options}
        valid_output_fields = set(output_labels)
        selected_output_fields = [
            str(field)
            for field in step.get("output_fields", [])
            if str(field) in valid_output_fields
        ]
        if not selected_output_fields:
            selected_output_fields = [item["field"] for item in output_options[:7]]
        steps.append({
            "index": index,
            "name": str(step.get("name", f"步骤{index}")),
            "right_table": right_table,
            "right_table_name": rule_tables.get(right_table, {}).get("name", right_table),
            "selected_fields": selected_fields,
            "selected_labels": [match_labels.get(field, _display_column_label(field)) for field in selected_fields],
            "match_options": match_options,
            "selected_output_fields": selected_output_fields,
            "selected_output_labels": [output_labels.get(field, _display_column_label(field)) for field in selected_output_fields],
            "output_options": output_options,
            "unmatched": str(step.get("unmatched", "mark_exception")),
        })
        left_fields = _merge_unique_fields(left_fields, selected_output_fields)
    return steps


def _flow_calculated_columns_config(flow: dict[str, Any]) -> list[dict[str, Any]]:
    if "calculated_columns" in flow:
        columns = [dict(item) for item in flow.get("calculated_columns", [])]
    else:
        columns = _default_calculated_columns()
    out = []
    for index, column in enumerate(columns, start=1):
        out.append({
            "index": index,
            "field": str(column.get("field", "")),
            "name": str(column.get("name", "")),
            "formula": str(column.get("formula", "")),
            "active": str(column.get("active", "Y")).upper() in {"Y", "YES", "TRUE", "1", "是"},
        })
    return out


def _calculated_columns_from_form(form: Any) -> list[dict[str, str]]:
    columns = []
    indexes = sorted({int(value) for value in form.getlist("calc_index") if str(value).isdigit()})
    for index in indexes:
        field = _safe_table_key(form.get(f"calc_{index}_field", ""))
        name = form.get(f"calc_{index}_name", "").strip()
        formula = form.get(f"calc_{index}_formula", "").strip()
        if not field or not formula:
            continue
        columns.append({
            "field": field,
            "name": name or _display_column_label(field),
            "formula": formula,
            "active": "Y" if form.get(f"calc_{index}_active") == "Y" else "N",
        })
    return columns


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
            "unmatched": "mark_exception",
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
            "unmatched": "mark_exception",
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
            "unmatched": "mark_exception",
        },
    ]


def _save_merge_material_fields(cfg: AppConfig, selected_fields: list[str], rule_table: str) -> None:
    raw = _load_merge_config(cfg)
    flows = raw.setdefault("flows", {})
    flow = flows.setdefault(MERGE_FLOW_KEY, {})
    flow.setdefault("name", "表1 SAP流水合并表2物料主数据")
    flow.setdefault("left_table", "raw_sap_monthly_data")
    flow["right_table"] = rule_table
    flow.setdefault("join_type", "left")
    flow.setdefault("active_col", "is_active")
    flow.setdefault("output_fields", [
        "brand",
        "model",
        "project_name",
        "factory_material_group",
        "model_category",
        "process_category",
        "standard_type",
    ])
    flow.setdefault("unmatched", "mark_exception")
    flow["priority_specs"] = [{
        "priority": 1,
        "keys": [{"left": field, "right": field} for field in selected_fields],
    }]
    _save_merge_config(cfg, raw)


def _update_merge_flow_config(
    cfg: AppConfig,
    flow_key: str,
    steps: list[dict[str, Any]],
    calculated_columns: list[dict[str, str]] | None = None,
) -> None:
    raw = _load_merge_config(cfg)
    flows = raw.setdefault("flows", {})
    flow = flows.setdefault(flow_key, {})
    flow.setdefault("name", flow_key)
    flow.setdefault("description", "")
    flow.setdefault("left_table", "raw_sap_monthly_data")
    first_step = steps[0]
    flow["right_table"] = first_step["right_table"]
    flow["join_type"] = "left"
    flow["active_col"] = "is_active"
    flow["priority_specs"] = [{
        "priority": 1,
        "keys": [{"left": field, "right": field} for field in first_step["match_fields"]],
    }]
    flow["output_fields"] = first_step["output_fields"]
    flow["unmatched"] = "mark_exception"
    flow["steps"] = [
        {
            "name": step["name"],
            "left_table": "raw_sap_monthly_data" if index == 1 else "previous_result",
            "right_table": step["right_table"],
            "join_type": "left",
            "unmatched": "mark_exception",
            "keys": [{"left": field, "right": field} for field in step["match_fields"]],
            "output_fields": step["output_fields"],
        }
        for index, step in enumerate(steps, start=1)
    ]
    flow["calculated_columns"] = calculated_columns or []
    _save_merge_config(cfg, raw)


def _rule_output_field_options(cfg: AppConfig, table_key: str) -> list[dict[str, str]]:
    return _field_options(_config_table_fields(cfg, table_key), exclude=_control_fields())


def _sap_fact_fields() -> list[str]:
    return [field for field in SAP_PREVIEW_LABELS if field not in {"raw_id", "source_row_no"}]


def _config_table_fields(cfg: AppConfig, table_key: str, version_id: str | None = None) -> list[str]:
    path = _config_version_file_path(cfg, table_key, version_id) if version_id else _active_config_table_path(cfg, table_key)
    columns: list[Any]
    try:
        df = normalize_common(read_table(path))
        columns = list(df.columns)
    except Exception:
        columns = []
    if not columns:
        columns = RULE_STANDARD_FIELDS.get(table_key, [])
    fields: list[str] = []
    seen: set[str] = set()
    for col in columns:
        canonical = _canonical_rule_field(col)
        field = canonical if canonical in RULE_PREVIEW_LABELS or canonical in SAP_PREVIEW_LABELS else str(col)
        if field not in seen:
            seen.add(field)
            fields.append(field)
    return fields


def _active_config_table_path(cfg: AppConfig, table_key: str) -> Path:
    versions = _config_table_versions(cfg, table_key)
    for version in versions:
        if version.get("status") == "ACTIVE":
            return Path(str(version["path"]))
    if versions:
        return Path(str(versions[0]["path"]))
    return _rule_file_path(cfg, table_key)


def _step_match_field_options(cfg: AppConfig, left_fields: list[str], table_key: str, version_id: str | None = None) -> list[dict[str, str]]:
    right_fields = set(_config_table_fields(cfg, table_key, version_id=version_id))
    common_fields = [field for field in left_fields if field in right_fields and field not in _control_fields()]
    return _field_options(common_fields)


def _step_output_field_options(cfg: AppConfig, table_key: str, selected_match_fields: list[str], version_id: str | None = None) -> list[dict[str, str]]:
    exclude = set(selected_match_fields) | _control_fields()
    fields = [field for field in _config_table_fields(cfg, table_key, version_id=version_id) if field not in exclude]
    return _field_options(fields)


def _field_options(fields: list[str], exclude: set[str] | None = None) -> list[dict[str, str]]:
    exclude = exclude or set()
    options = []
    seen: set[str] = set()
    for field in fields:
        if field in exclude or field in seen:
            continue
        seen.add(field)
        options.append({"field": field, "label": _display_column_label(field)})
    return options


def _control_fields() -> set[str]:
    return {"is_active", "priority", "created_at", "created_by", "data_source"}


def _merge_unique_fields(left: list[str], right: list[str]) -> list[str]:
    out = list(left)
    seen = set(out)
    for field in right:
        if field not in seen:
            seen.add(field)
            out.append(field)
    return out


def _merge_flows_summary(cfg: AppConfig) -> list[dict[str, Any]]:
    raw = _load_merge_config(cfg)
    flows = raw.get("flows", {})
    out = []
    rule_tables = _rule_tables(cfg)
    for key, flow in flows.items():
        steps = [dict(step) for step in flow.get("steps", []) if step.get("right_table")]
        first_keys = steps[0].get("keys", []) if steps else []
        field_labels = [
            _display_column_label(item.get("left", ""))
            for item in first_keys
            if item.get("left")
        ]
        right_table = str((steps[0] if steps else flow).get("right_table", ""))
        out.append({
            "key": str(key),
            "name": str(flow.get("name", key)),
            "description": str(flow.get("description", "")),
            "main_table": str(flow.get("left_table", "表1 SAP流水")),
            "right_table": right_table,
            "right_table_name": rule_tables.get(right_table, {}).get("name", right_table),
            "join_type": str(flow.get("join_type", "left")),
            "field_labels": field_labels,
            "field_count": len(field_labels),
            "output_count": len((steps[0] if steps else flow).get("output_fields", [])),
            "unmatched": str(flow.get("unmatched", "mark_exception")),
        })
    return out


def _save_new_merge_flow(
    cfg: AppConfig,
    flow_key: str,
    name: str,
    description: str,
    main_table: str,
    right_table: str,
    step_name: str,
    selected_fields: list[str],
    output_fields: list[str],
    extra_steps: list[dict[str, Any]] | None = None,
) -> None:
    raw = _load_merge_config(cfg)
    flows = raw.setdefault("flows", {})
    steps_payload = [{
        "name": step_name,
        "left_table": main_table,
        "right_table": right_table,
        "join_type": "left",
        "unmatched": "mark_exception",
        "keys": [{"left": field, "right": field} for field in selected_fields],
        "output_fields": output_fields,
    }]
    for step in extra_steps or []:
        steps_payload.append({
            "name": step["name"],
            "left_table": "previous_result",
            "right_table": step["right_table"],
            "join_type": "left",
            "unmatched": "mark_exception",
            "keys": [{"left": field, "right": field} for field in step["match_fields"]],
            "output_fields": step["output_fields"],
        })
    flows[flow_key] = {
        "name": name,
        "description": description,
        "left_table": main_table,
        "right_table": right_table,
        "join_type": "left",
        "active_col": "is_active",
        "steps": steps_payload,
        "calculated_columns": _default_calculated_columns(),
        "priority_specs": [{
            "priority": 1,
            "keys": [{"left": field, "right": field} for field in selected_fields],
        }],
        "output_fields": output_fields,
        "unmatched": "mark_exception",
    }
    _save_merge_config(cfg, raw)


def _delete_sap_batch(conn: sqlite3.Connection, import_batch_id: str) -> None:
    merge_ids = [row[0] for row in conn.execute("SELECT merge_run_id FROM merge_run_log WHERE import_batch_id=?", (import_batch_id,)).fetchall()]
    run_ids = [row[0] for row in conn.execute("SELECT run_id FROM calc_run_log WHERE import_batch_id=?", (import_batch_id,)).fetchall()]
    conn.execute("DELETE FROM raw_sap_monthly_data WHERE import_batch_id=?", (import_batch_id,))
    conn.execute("DELETE FROM sap_import_batch WHERE import_batch_id=?", (import_batch_id,))
    for merge_id in merge_ids:
        conn.execute("DELETE FROM merge_result WHERE merge_run_id=?", (merge_id,))
        conn.execute("DELETE FROM merge_exception WHERE merge_run_id=?", (merge_id,))
        conn.execute("DELETE FROM merge_run_log WHERE merge_run_id=?", (merge_id,))
    for run_id in run_ids:
        conn.execute("DELETE FROM calc_result WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM calc_exception WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM rule_table_snapshot WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM calc_run_log WHERE run_id=?", (run_id,))
    conn.commit()


def _delete_merge_run(conn: sqlite3.Connection, cfg: AppConfig, merge_run_id: str) -> None:
    row = conn.execute("SELECT result_file, exception_file FROM merge_run_log WHERE merge_run_id=?", (merge_run_id,)).fetchone()
    conn.execute("DELETE FROM merge_result WHERE merge_run_id=?", (merge_run_id,))
    conn.execute("DELETE FROM merge_exception WHERE merge_run_id=?", (merge_run_id,))
    conn.execute("DELETE FROM merge_run_log WHERE merge_run_id=?", (merge_run_id,))
    conn.commit()
    if row:
        for value in row:
            path = Path(str(value or ""))
            if path.exists() and path.resolve().is_relative_to(cfg.output_dir.resolve()):
                path.unlink()


def _backup_existing_file(path: Path) -> None:
    if not path.exists():
        return
    backup_dir = path.parent / "_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{path.stem}_{stamp}{path.suffix}"
    backup_path.write_bytes(path.read_bytes())


BASE_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>工厂加工费管理台</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #202124;
      --muted: #5f6368;
      --line: #d7dce2;
      --panel: #ffffff;
      --band: #f4f7f9;
      --accent: #166534;
      --accent-dark: #14532d;
      --warn: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      background: var(--band);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }
    header { background: #19332d; color: white; border-bottom: 1px solid #10231e; }
    .header-inner, main { width: min(1680px, calc(100vw - 36px)); margin: 0 auto; }
    .header-inner { display: flex; align-items: center; justify-content: space-between; min-height: 64px; gap: 24px; }
    .brand { display: grid; gap: 2px; font-size: 20px; font-weight: 700; }
    .brand small { font-size: 12px; font-weight: 500; color: #c7d8d1; }
    nav { display: flex; gap: 6px; flex-wrap: wrap; }
    nav a {
      color: #dfe9e4;
      text-decoration: none;
      padding: 8px 12px;
      border-radius: 6px;
      line-height: 1;
    }
    nav a.active, nav a:hover { background: rgba(255,255,255,.14); color: #fff; }
    main { padding: 24px 0 44px; }
    h1 { font-size: 24px; margin: 0 0 8px; }
    h2 { font-size: 18px; margin: 0 0 14px; }
    p { color: var(--muted); margin: 0; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin: 18px 0; }
    .panel, .metric, .flash {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(0,0,0,.04);
    }
    .panel { padding: 18px; margin-top: 16px; overflow: hidden; }
    .metric { padding: 16px; }
    .metric strong { display: block; font-size: 28px; margin-top: 6px; }
    .form-row { display: grid; grid-template-columns: minmax(120px, 180px) minmax(0, 1fr); gap: 12px; align-items: center; margin: 12px 0; }
    label { color: #3c4043; font-weight: 600; }
    input[type=text], input[type=file], select {
      width: 100%;
      min-height: 38px;
      border: 1px solid #b8c2cc;
      border-radius: 6px;
      padding: 8px 10px;
      background: white;
      color: var(--ink);
    }
    button, .button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      padding: 0 14px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      background: var(--accent);
      color: white;
      font-weight: 650;
      text-decoration: none;
      cursor: pointer;
      white-space: nowrap;
    }
    button:hover, .button:hover { background: var(--accent-dark); }
    .button.secondary { background: white; color: var(--accent); }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 14px; }
    .button-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 14px; }
    .button-row form { margin: 0; }
    .inline-form { display: inline-flex; margin: 0; gap: 8px; align-items: center; }
    table { width: max-content; min-width: 100%; border-collapse: collapse; table-layout: auto; }
    th, td {
      border-bottom: 1px solid #e5e9ee;
      padding: 9px 10px;
      text-align: left;
      vertical-align: top;
      line-height: 1.35;
      white-space: nowrap;
    }
    th { background: #edf3f0; color: #33413c; font-weight: 700; position: sticky; top: 0; }
    th, td { white-space: nowrap; min-width: 96px; }
    td { max-width: 420px; overflow: hidden; text-overflow: ellipsis; }
    .table-wrap { overflow: auto; border: 1px solid var(--line); border-radius: 8px; max-height: min(68vh, 720px); }
    .table-wrap.dense th, .table-wrap.dense td { padding: 7px 8px; }
    .muted { color: var(--muted); }
    .flash { padding: 12px 14px; margin: 0 0 12px; }
    .flash.success { border-color: #9dd4ad; background: #effaf2; }
    .flash.error { border-color: #f1a7a0; background: #fff3f2; color: var(--warn); }
    .rule-layout { display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 16px; align-items: start; }
    .rule-list { display: grid; gap: 8px; }
    .rule-list a {
      display: block;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
      color: var(--ink);
      text-decoration: none;
    }
    .rule-list a.active { border-color: var(--accent); box-shadow: inset 3px 0 0 var(--accent); }
    .rule-list small { display: block; color: var(--muted); margin-top: 5px; line-height: 1.35; }
    .tabs { display: flex; gap: 8px; margin-top: 18px; border-bottom: 1px solid var(--line); }
    .tabs a {
      padding: 10px 14px;
      color: var(--muted);
      text-decoration: none;
      border: 1px solid transparent;
      border-bottom: 0;
      border-radius: 6px 6px 0 0;
      font-weight: 700;
    }
    .tabs a.active { background: white; color: var(--accent); border-color: var(--line); }
    .split-layout { display: grid; grid-template-columns: clamp(240px, 20vw, 320px) minmax(0, 1fr); gap: 16px; align-items: start; margin-top: 16px; }
    .progressive-workspace {
      display: grid;
      grid-template-columns: 220px 260px minmax(620px, 1fr) minmax(420px, 520px);
      gap: 14px;
      align-items: start;
      overflow-x: auto;
      padding-bottom: 10px;
      margin-top: 16px;
    }
    .stage-column {
      min-height: calc(100vh - 230px);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      overflow: auto;
    }
    .stage-column h2 { font-size: 16px; margin-bottom: 10px; }
    .stage-column.compact {
      width: 220px;
    }
    .stage-column.canvas {
      background: #fbfdfc;
    }
    .stage-column.detail {
      max-height: calc(100vh - 230px);
    }
    .flow-card {
      display: block;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
      color: var(--ink);
      text-decoration: none;
      margin-top: 8px;
    }
    .flow-card.active { border-color: var(--accent); box-shadow: inset 3px 0 0 var(--accent); }
    .flow-setting-layout { display: grid; gap: 14px; margin-top: 16px; }
    .flow-top, .flow-bottom, .flow-middle > section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      overflow: hidden;
    }
    .flow-middle { display: grid; grid-template-columns: minmax(260px, 360px) minmax(0, 1fr); gap: 14px; align-items: stretch; }
    .flow-middle > section { min-height: 300px; }
    .flow-scroll { overflow-x: auto; padding-bottom: 8px; }
    .step-card { border: 1px solid var(--line); border-radius: 8px; padding: 14px; margin-top: 12px; background: white; }
    .flow-workbench { display: grid; grid-template-columns: 260px minmax(360px, 1fr) 360px; gap: 16px; align-items: start; margin-top: 16px; }
    .step-list { display: grid; gap: 8px; }
    .step-pill { display: block; border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; background: #fff; color: var(--ink); text-decoration: none; }
    .step-pill strong { display: block; }
    .flow-diagram {
      display: flex;
      align-items: stretch;
      gap: 0;
      min-width: max-content;
      padding: 8px 4px 18px;
    }
    .flow-chain { overflow-x: auto; padding-bottom: 8px; white-space: nowrap; }
    .flow-node { display: inline-block; vertical-align: middle; min-width: 220px; max-width: 280px; white-space: normal; border: 1px solid var(--line); border-radius: 8px; background: #f8faf9; padding: 14px; }
    .diagram-node {
      width: 240px;
      flex: 0 0 240px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
      padding: 14px;
      box-shadow: 0 1px 2px rgba(0,0,0,.04);
    }
    .diagram-node.source { border-color: #9fb7aa; background: #f4faf6; }
    .diagram-node strong { display: block; margin-bottom: 8px; font-size: 15px; }
    .diagram-node span { display: block; color: var(--muted); line-height: 1.45; margin-top: 4px; white-space: normal; }
    .diagram-arrow {
      flex: 0 0 58px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--accent);
      font-size: 24px;
      font-weight: 800;
    }
    .flow-node strong { display: block; margin-bottom: 6px; }
    .flow-node span { display: block; margin-top: 4px; color: var(--muted); }
    .flow-arrow { display: inline-block; vertical-align: middle; padding: 0 8px; color: var(--muted); font-weight: 700; }
    .summary-list { display: grid; gap: 10px; margin-top: 12px; }
    .summary-item { border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fbfdfc; }
    .version-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin: 12px 0; }
    .version-meta div { border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #fbfdfc; }
    .version-card {
      display: block;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      margin-top: 8px;
      color: var(--ink);
      text-decoration: none;
      background: #fff;
    }
    .version-card.active { border-color: var(--accent); box-shadow: inset 3px 0 0 var(--accent); }
    .version-card strong { display: block; }
    .compact-form .form-row { grid-template-columns: 1fr; gap: 6px; margin: 10px 0; }
    .drawer-backdrop { position: fixed; inset: 0; background: rgba(32,33,36,.42); z-index: 20; display: flex; justify-content: center; align-items: flex-start; padding: 24px; }
    .drawer {
      width: min(1320px, calc(100vw - 48px));
      height: min(920px, calc(100vh - 48px));
      background: white;
      padding: 22px;
      overflow: auto;
      border-radius: 10px;
      box-shadow: 0 18px 60px rgba(0,0,0,.22);
    }
    .drawer-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; border-bottom: 1px solid var(--line); padding-bottom: 12px; margin-bottom: 16px; }
    .step-tabs { display: flex; gap: 8px; color: var(--muted); font-weight: 700; margin: 12px 0; }
    .step-tabs span { padding: 6px 10px; border: 1px solid var(--line); border-radius: 999px; background: #f8faf9; }
    .optional-step[hidden] { display: none; }
    .calc-column-list { display: grid; gap: 10px; margin-top: 12px; }
    .calc-column-row {
      display: grid;
      grid-template-columns: 92px minmax(120px, 180px) minmax(150px, 220px) minmax(320px, 1fr) auto;
      gap: 10px;
      align-items: center;
    }
    .checkbox-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(116px, 1fr)); gap: 10px; margin-top: 12px; }
    .check-item {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 36px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      font-weight: 650;
      white-space: nowrap;
    }
    .check-item input { width: 16px; height: 16px; }
    @media (min-width: 1400px) {
      .checkbox-grid { grid-template-columns: repeat(auto-fit, minmax(116px, 1fr)); }
      .panel { padding: 20px; }
    }
    @media (max-width: 900px) {
      .header-inner { align-items: flex-start; flex-direction: column; padding: 14px 0; gap: 12px; }
      .grid, .rule-layout, .split-layout, .form-row { grid-template-columns: 1fr; }
      .progressive-workspace { grid-template-columns: 200px 240px minmax(560px, 1fr) minmax(380px, 460px); overflow-x: auto; }
      .stage-column { min-height: 520px; max-height: none; }
      .flow-middle { grid-template-columns: 1fr; }
      .flow-workbench { grid-template-columns: 1fr; }
      .calc-column-row { grid-template-columns: 1fr; }
      .checkbox-grid { grid-template-columns: 1fr; }
      main { width: min(100vw - 20px, 1680px); }
      .table-wrap { max-height: 62vh; }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <div class="brand">工厂加工费管理平台<small>演示版，Codex生成</small></div>
      <nav>
        <a class="{{ 'active' if page == 'sap' else '' }}" href="{{ url_for('sap_data', period=selected_period or cfg.yyyymm) }}">1 事实表管理</a>
        <a class="{{ 'active' if page == 'rules' and rules_tab == 'tables' else '' }}" href="{{ url_for('config_tables', period=selected_period or cfg.yyyymm) }}">2 配置表管理</a>
        <a class="{{ 'active' if page == 'rules' and rules_tab == 'flows' else '' }}" href="{{ url_for('flows', period=selected_period or cfg.yyyymm) }}">3 计算流程设置</a>
        <a class="{{ 'active' if page == 'dashboard' else '' }}" href="{{ url_for('index', period=selected_period or cfg.yyyymm) }}">4 执行计算</a>
        <a class="{{ 'active' if page == 'results' else '' }}" href="{{ url_for('results', period=selected_period or cfg.yyyymm) }}">5 计算结果</a>
        <a class="{{ 'active' if page == 'exports' else '' }}" href="{{ url_for('exports') }}">6 加工费导出</a>
        <a class="{{ 'active' if page == 'runs' else '' }}" href="{{ url_for('runs', period=selected_period or cfg.yyyymm) }}">日志</a>
        <a class="{{ 'active' if page == 'help' else '' }}" href="{{ url_for('help_page', period=selected_period or cfg.yyyymm) }}">使用指引</a>
      </nav>
    </div>
  </header>
  <main>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category, message in messages %}
        <div class="flash {{ category }}">{{ message }}</div>
      {% endfor %}
    {% endwith %}

    {% if page == 'settings' %}
      <h1>基础配置</h1>
      <p>集中管理期间。业务页面只消费当前期间，不再把期间操作散落到每个页面。</p>
      <section class="panel" style="margin-top:0">
        <h2>期间管理</h2>
        <div class="button-row" style="margin-top:0">
        <form method="get" action="" class="inline-form">
          <label for="period_picker">期间</label>
          <select id="period_picker" name="period" style="max-width:220px" onchange="this.form.submit()">
            {% for p in periods %}
              <option value="{{ p.period_id }}" {% if p.period_id == selected_period %}selected{% endif %}>{{ p.period_id }}｜{{ p.status_label }}</option>
            {% endfor %}
          </select>
          {% for p in periods if p.period_id == selected_period %}
            <span class="muted">当前状态：{{ p.status_label }}</span>
          {% endfor %}
        </form>
        <form class="inline-form" method="post" action="{{ url_for('update_period_status', period_id=selected_period, action='complete', period=selected_period) }}"><button class="button secondary" type="submit">标记完成</button></form>
        <form class="inline-form" method="post" action="{{ url_for('update_period_status', period_id=selected_period, action='close', period=selected_period) }}"><button class="button secondary" type="submit">封存期间</button></form>
        <form class="inline-form" method="post" action="{{ url_for('update_period_status', period_id=selected_period, action='archive', period=selected_period) }}"><button class="button secondary" type="submit">归档期间</button></form>
        <form class="inline-form" method="post" action="{{ url_for('update_period_status', period_id=selected_period, action='reopen', period=selected_period) }}"><button class="button secondary" type="submit">重新打开</button></form>
        <form id="new-period" method="post" action="{{ url_for('create_period') }}" class="inline-form">
          <input id="new_period_id" name="period_id" type="text" placeholder="新增期间，例如 202506" style="max-width:220px">
          <button type="submit">创建期间</button>
        </form>
        </div>
      </section>
    {% endif %}

    {% if page == 'dashboard' %}
      <h1>执行计算</h1>
      <p>选择期间SAP事实表、计算流程，并为每个步骤选择配置表版本。</p>
      <div class="grid">
        <div class="metric"><span class="muted">计算结果行</span><strong>{{ summary.counts.calc_result }}</strong></div>
        <div class="metric"><span class="muted">异常日志行</span><strong>{{ summary.counts.calc_exception }}</strong></div>
        <div class="metric"><span class="muted">合并结果行</span><strong>{{ summary.counts.merge_result }}</strong></div>
      </div>
      <section class="panel">
        <h2>计算任务中心</h2>
        <form method="post" action="{{ url_for('run') }}">
          <input type="hidden" name="period_id" value="{{ selected_period or cfg.yyyymm }}">
          <div class="form-row" style="display:none">
            <label for="yyyymm">计算年月</label>
            <input id="yyyymm" name="yyyymm" type="text" value="{{ cfg.yyyymm }}" placeholder="例如 202502">
          </div>
          <div class="form-row">
            <label for="import_batch_id">SAP 数据批次</label>
            <select id="import_batch_id" name="import_batch_id">
              <option value="">请选择SAP导入批次</option>
              {% for batch in batches %}
                <option value="{{ batch.import_batch_id }}">{{ batch.import_batch_id }}｜{{ batch.yyyymm }}｜{{ batch.source_file_name }}｜{{ batch.import_rows }} 行</option>
              {% endfor %}
            </select>
          </div>
          <div class="form-row">
            <label for="flow_key">计算流程</label>
            <select id="flow_key" name="flow_key" onchange="window.location='{{ url_for('index', period=selected_period) }}&flow=' + this.value">
              {% for flow in merge_flows %}
                <option value="{{ flow.key }}" {% if flow.key == selected_flow_key %}selected{% endif %}>{{ flow.name }}</option>
              {% endfor %}
            </select>
          </div>
          {% for step in execution_config.steps %}
            <div class="form-row">
              <label>{{ step.name }}版本</label>
              <select name="config_version_{{ step.right_table }}">
                {% for version in step.versions %}
                  <option value="{{ version.version_id }}" {% if version.version_id == step.selected_version %}selected{% endif %}>{{ step.rule_table_name }}｜{{ version.name }}</option>
                {% else %}
                  <option value="">请先在配置表管理新增版本</option>
                {% endfor %}
              </select>
            </div>
          {% endfor %}
          <div class="summary-list">
            <h2>本次计算清单</h2>
            <div class="summary-item"><strong>期间事实表</strong><p>{{ selected_period }}｜{% if execution_summary.default_batch %}默认批次 {{ execution_summary.default_batch.import_batch_id }}{% else %}请先到事实表管理导入SAP数据{% endif %}</p></div>
            <div class="summary-item"><strong>计算流程</strong><p>{{ execution_config.name }}</p></div>
            {% for step in execution_config.steps %}
              <div class="summary-item"><strong>{{ step.name }}</strong><p>{{ step.rule_table_name }}｜默认选择：{% if step.versions %}{{ step.versions[0].name }}{% else %}暂无版本{% endif %}</p></div>
            {% endfor %}
          </div>
          <div class="actions">
            <button type="submit">开始计算</button>
            <a class="button secondary" href="{{ url_for('sap_data', period=selected_period) }}">事实表管理</a>
            <a class="button secondary" href="{{ url_for('config_tables', period=selected_period) }}">维护配置表版本</a>
          </div>
        </form>
      </section>
      <section class="panel">
        <h2>最近合并</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>合并运行ID</th><th>流程</th><th>SAP导入批次</th><th>输入行数</th><th>输出行数</th><th>异常行数</th><th>状态</th><th>开始时间</th><th>完成时间</th><th>操作</th></tr></thead>
            <tbody>
              {% for row in summary.merge_runs %}
                <tr>
                  <td>{{ row.merge_run_id }}</td><td>{{ row.flow_name }}</td><td>{{ row.import_batch_id }}</td><td>{{ row.left_rows }}</td><td>{{ row.merged_rows }}</td><td>{{ row.exception_rows }}</td><td>{{ row.status_label }}</td><td>{{ row.started_at }}</td><td>{{ row.finished_at }}</td>
                  <td><a href="{{ url_for('merge_run_detail', merge_run_id=row.merge_run_id) }}">查看</a></td>
                </tr>
              {% else %}
                <tr><td colspan="10" class="muted">暂无合并记录</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>最近运行</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>计算运行ID</th><th>SAP导入批次</th><th>SAP 文件</th><th>SAP 行数</th><th>结果行数</th><th>异常行数</th><th>年月</th><th>工厂</th><th>状态</th><th>开始时间</th><th>完成时间</th></tr></thead>
            <tbody>
              {% for row in summary.runs %}
                <tr><td>{{ row.run_id }}</td><td>{{ row.import_batch_id }}</td><td>{{ row.sap_file_name }}</td><td>{{ row.sap_import_rows }}</td><td>{{ row.calc_result_rows }}</td><td>{{ row.calc_exception_rows }}</td><td>{{ row.yyyymm }}</td><td>{{ row.factory_scope }}</td><td>{{ row.status_label }}</td><td>{{ row.started_at }}</td><td>{{ row.finished_at }}</td></tr>
              {% else %}
                <tr><td colspan="11" class="muted">暂无运行记录</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </section>
    {% elif page == 'sap' %}
      <h1>事实表管理</h1>
      <p>上传 SAP 月度加工费流水，生成 SAP 导入批次，并写入本地数据库。</p>
      <section class="panel">
        <h2>导入 SAP 流水</h2>
        <form method="post" enctype="multipart/form-data" action="{{ url_for('upload_sap') }}">
          <input type="hidden" name="period_id" value="{{ selected_period or cfg.yyyymm }}">
          <div class="form-row">
            <label for="sap_file">SAP 文件</label>
            <input id="sap_file" name="file" type="file" accept=".csv,.xlsx,.xls">
          </div>
          <div class="form-row" style="display:none">
            <label for="sap_yyyymm">计算年月</label>
            <input id="sap_yyyymm" name="yyyymm" type="text" value="{{ cfg.yyyymm }}" placeholder="例如 2025-02 或 202502">
          </div>
          <div class="form-row" style="display:none">
            <label for="sap_factory_scope">工厂范围</label>
            <input id="sap_factory_scope" name="factory_scope" type="text" value="{{ ','.join(cfg.factory_scope) }}" placeholder="例如 F001,F002；留空表示不过滤">
          </div>
          <div class="actions">
            <button type="submit">导入数据库</button>
          </div>
        </form>
      </section>
      <section class="panel">
        <h2>已导入批次</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>SAP导入批次</th><th>年月</th><th>工厂范围</th><th>文件名</th><th>导入行数</th><th>导入时间</th><th>状态</th><th>预览</th></tr></thead>
            <tbody>
              {% for batch in batches %}
                <tr>
                  <td>{{ batch.import_batch_id }}</td><td>{{ batch.yyyymm }}</td><td>{{ batch.factory_scope }}</td><td>{{ batch.source_file_name }}</td><td>{{ batch.import_rows }}</td><td>{{ batch.imported_at }}</td><td>{{ batch.status_label }}</td>
                  <td>
                    <a href="{{ url_for('sap_data', period=selected_period, import_batch_id=batch.import_batch_id) }}">查看</a>
                    <form method="post" action="{{ url_for('delete_sap_batch', import_batch_id=batch.import_batch_id, period=selected_period) }}" style="display:inline"><button class="button secondary" type="submit">删除</button></form>
                  </td>
                </tr>
              {% else %}
                <tr><td colspan="8" class="muted">暂无 SAP 导入批次</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>数据预览</h2>
        <p>{% if selected_batch %}当前批次：{{ selected_batch }}，共 {{ preview.row_count }} 行，预览前 20 行。{% else %}暂无可预览批次。{% endif %}</p>
        <div class="table-wrap">
          <table>
            <thead><tr>{% for col in preview.columns %}<th>{{ col }}</th>{% endfor %}</tr></thead>
            <tbody>
              {% for row in preview.rows %}
                <tr>{% for col in preview.columns %}<td>{{ row[col] }}</td>{% endfor %}</tr>
              {% else %}
                <tr><td class="muted">暂无可预览数据</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </section>
    {% elif page == 'rules' %}
      <h1>{% if rules_tab == 'tables' %}配置表管理{% else %}计算流程设置{% endif %}</h1>
      <p>{% if rules_tab == 'tables' %}管理表2、表3、表4、表5以及表N的多个版本，版本可跨期间复用。{% else %}流程只定义步骤、匹配字段、输出字段，并引用配置表类型，不绑定具体版本。{% endif %}</p>

      {% if rules_tab == 'tables' %}
        <div class="progressive-workspace" style="grid-template-columns: 260px 320px minmax(760px, 1fr);">
          <aside class="stage-column">
            <h2>配置表类型</h2>
            <div class="rule-list">
            {% for key, meta in rule_tables.items() %}
              <a class="{{ 'active' if selected == key else '' }}" href="{{ url_for('rules', tab='tables', table=key, period=selected_period) }}">
                <strong>{{ meta.name }}</strong>
                <small>{{ meta.description }}</small>
              </a>
            {% else %}
              <div class="version-card"><strong>暂无配置表类型</strong><span class="muted">请在下方新增配置表类型。</span></div>
            {% endfor %}
            </div>
            {% if rule_tables %}
            <div class="button-row">
              <form class="inline-form" method="post" action="{{ url_for('move_rule_table_type', table_key=selected, direction='up', period=selected_period) }}"><button class="button secondary" type="submit">上移</button></form>
              <form class="inline-form" method="post" action="{{ url_for('move_rule_table_type', table_key=selected, direction='down', period=selected_period) }}"><button class="button secondary" type="submit">下移</button></form>
              <form class="inline-form" method="post" action="{{ url_for('delete_rule_table_type', table_key=selected, period=selected_period) }}"><button class="button secondary" type="submit">删除类型</button></form>
            </div>
            {% endif %}
            <section id="new-rule-table" class="compact-form" style="margin-top:18px">
              <h2>新增配置表类型</h2>
              <form method="post" action="{{ url_for('create_rule_table') }}">
                <div class="form-row">
                  <label for="new_table_key">简称</label>
                  <input id="new_table_key" name="table_key" type="text" placeholder="custom_model_map">
                </div>
                <div class="form-row">
                  <label for="new_table_name">名称</label>
                  <input id="new_table_name" name="name" type="text" placeholder="自定义机型映射表">
                </div>
                <div class="form-row">
                  <label for="new_table_desc">说明</label>
                  <input id="new_table_desc" name="description" type="text" placeholder="这张表的用途">
                </div>
                <div class="button-row">
                  <button type="submit">新增类型</button>
                </div>
              </form>
            </section>
          </aside>
          <aside class="stage-column">
            <h2>配置表版本</h2>
            {% if rule_tables %}
            <p>{{ rule_tables[selected].name }}</p>
            {% for version in config_versions %}
              <a class="version-card {{ 'active' if version.version_id == selected_version else '' }}" href="{{ url_for('rules', tab='tables', table=selected, version=version.version_id, period=selected_period) }}">
                <strong>{{ version.name }}</strong>
                <span class="muted">{{ version.version_id }}｜{{ version.status_label }}</span>
                <span class="muted">{{ version.created_by or 'admin' }}｜{{ version.created_at or '' }}</span>
              </a>
            {% else %}
              <div class="version-card"><strong>暂无版本</strong><span class="muted">请在下方上传新增</span></div>
            {% endfor %}
            <form class="compact-form" method="post" enctype="multipart/form-data" action="{{ url_for('upload_rule', table_key=selected) }}" style="margin-top:18px">
              <input type="hidden" name="period" value="{{ selected_period }}">
              <h2>新增配置表版本</h2>
              <div class="form-row">
                <label for="version_id">版本编码</label>
                <input id="version_id" name="version_id" type="text" placeholder="例如 202502_v1">
              </div>
              <div class="form-row">
                <label for="version_name">版本名称</label>
                <input id="version_name" name="version_name" type="text" placeholder="例如 202502 正式版">
              </div>
              <div class="form-row">
                <label for="version_description">版本说明</label>
                <input id="version_description" name="version_description" type="text" placeholder="说明这个版本适用场景">
              </div>
              <div class="form-row">
                <label for="created_by">创建人</label>
                <input id="created_by" name="created_by" type="text" value="admin">
              </div>
              <div class="form-row">
                <label for="file">上传规则文件</label>
                <input id="file" name="file" type="file" accept=".csv,.xlsx,.xls">
              </div>
              <div class="button-row">
                <button type="submit">新增配置表版本</button>
              </div>
            </form>
            {% else %}
              <div class="version-card"><strong>暂无版本</strong><span class="muted">新增配置表类型后再上传版本。</span></div>
            {% endif %}
          </aside>
          <section class="stage-column detail">
            {% if rule_tables %}
            <h2>{{ rule_tables[selected].name }}详情</h2>
            <p>{{ rule_tables[selected].description }}</p>
            {% for version in config_versions if version.version_id == selected_version %}
              <div class="version-meta">
                <div><strong>版本名称</strong><p>{{ version.name }}</p></div>
                <div><strong>版本说明</strong><p>{{ version.description or '无' }}</p></div>
                <div><strong>创建人</strong><p>{{ version.created_by or 'admin' }}</p></div>
                <div><strong>创建时间</strong><p>{{ version.created_at or '' }}</p></div>
                <div><strong>状态</strong><p>{{ version.status_label }}</p></div>
              </div>
              <div class="actions">
                <span class="muted">文件：{{ preview.path }}</span>
                <span class="muted">总行数：{{ preview.row_count }}</span>
              </div>
              <div class="button-row">
                <form class="inline-form" method="post" action="{{ url_for('update_config_version_status', table_key=selected, version_id=selected_version, period=selected_period) }}">
                  <input type="hidden" name="status" value="{{ 'INACTIVE' if version.status == 'ACTIVE' else 'ACTIVE' }}">
                  <button class="button secondary" type="submit">{{ '停用版本' if version.status == 'ACTIVE' else '启用版本' }}</button>
                </form>
                {% if preview.exists %}
                  <a class="button secondary" href="{{ url_for('download_rule', table_key=selected, version=selected_version, period=selected_period) }}">下载当前版本</a>
                  <form class="inline-form" method="post" action="{{ url_for('delete_rule_table_data', table_key=selected) }}"><input type="hidden" name="period" value="{{ selected_period }}"><input type="hidden" name="version" value="{{ selected_version }}"><button class="button secondary" type="submit">删除当前版本</button></form>
                {% endif %}
              </div>
            {% endfor %}
            <div class="actions"><span class="muted">预览前 30 行，标准字段在前，额外字段排在后续列。</span></div>
            <div class="table-wrap">
              <table>
                <thead><tr>{% for col in preview.columns %}<th>{{ col }}</th>{% endfor %}</tr></thead>
                <tbody>
                  {% for row in preview.rows %}
                    <tr>{% for col in preview.columns %}<td>{{ row[col] }}</td>{% endfor %}</tr>
                  {% else %}
                    <tr><td class="muted">暂无可预览数据</td></tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
            {% else %}
              <h2>配置表详情</h2>
              <p>当前没有任何配置表类型。请先在左侧新增一个配置表类型，再上传对应版本。</p>
            {% endif %}
          </section>
        </div>
      {% else %}
        {% if merge_flows %}
        <form id="flow-config-form" method="post" action="{{ url_for('update_merge_flow', flow_key=flow_config.flow_key) }}">
          <input type="hidden" name="period" value="{{ selected_period }}">
          <div class="flow-setting-layout">
            <section class="flow-top">
              <div class="button-row" style="margin-top:0">
                <strong>流程选择</strong>
                <select onchange="window.location='{{ url_for('rules', tab='flows', period=selected_period) }}&flow=' + this.value" style="max-width:420px">
                  {% for flow in merge_flows %}
                    <option value="{{ flow.key }}" {% if flow.key == selected_flow_key %}selected{% endif %}>{{ flow.name }}</option>
                  {% endfor %}
                </select>
                <a class="button" href="{{ url_for('rules', tab='flows', new_flow='1', period=selected_period) }}">新增流程</a>
                <button class="button secondary" type="submit" formaction="{{ url_for('delete_merge_flow', flow_key=flow_config.flow_key, period=selected_period) }}" formmethod="post">删除流程</button>
                <button type="submit">保存流程配置</button>
              </div>
            </section>
            <div class="flow-middle">
              <section>
                <div class="button-row" style="margin-top:0">
                  <h2 style="margin:0">步骤</h2>
                </div>
                <div class="step-list">
                  {% for step in flow_config.steps %}
                    <a class="step-pill" href="#step-{{ step.index }}">
                      <strong>步骤{{ step.index }}｜{{ step.name }}</strong>
                      <span class="muted">{{ step.right_table_name }}｜{{ step.selected_fields|length }} 个匹配字段</span>
                    </a>
                  {% else %}
                    <div class="step-pill"><strong>暂无步骤</strong><span class="muted">请新增流程步骤</span></div>
                  {% endfor %}
                </div>
              </section>
              <section>
                <div class="button-row" style="margin-top:0">
                  <h2 style="margin:0">流程图</h2>
                  <button class="button secondary" type="submit" formaction="{{ url_for('add_merge_flow_step', flow_key=flow_config.flow_key, period=selected_period) }}" formmethod="post">新增节点</button>
                </div>
                <p>{{ flow_config.description or '流程只绑定配置表类型，运行时再选择具体版本。' }}</p>
                <div class="flow-scroll">
                  <div class="flow-diagram">
                    <div class="diagram-node source">
                      <strong>事实表管理</strong>
                      <span>期间事实表</span>
                      <span>运行时选择批次</span>
                    </div>
                    {% for step in flow_config.steps %}
                      <div class="diagram-arrow">→</div>
                      <div class="diagram-node">
                        <strong>步骤{{ step.index }}：{{ step.name }}</strong>
                        <span>配置表类型：{{ step.right_table_name }}</span>
                        <span>匹配：{{ step.selected_labels|join('、') or '未配置' }}</span>
                        <span>输出：{{ step.selected_output_labels|join('、') or '未配置' }}</span>
                        <span>异常：记录异常并保留原行</span>
                      </div>
                    {% endfor %}
                  </div>
                </div>
              </section>
            </div>
            <section class="flow-bottom">
              <div class="button-row" style="margin-top:0">
                <h2 style="margin:0">步骤详情</h2>
                <button type="submit">保存步骤</button>
              </div>
              {% for step in flow_config.steps %}
                <div class="step-card" id="step-{{ step.index }}">
                  <input type="hidden" name="step_index" value="{{ step.index }}">
                  <div class="button-row" style="margin-top:0">
                    <h2 style="margin:0">步骤{{ step.index }}：{{ step.name }}</h2>
                    <button class="button secondary" type="submit" formaction="{{ url_for('delete_merge_flow_step', flow_key=flow_config.flow_key, step_index=step.index, period=selected_period) }}" formmethod="post">删除步骤</button>
                  </div>
                  <p>左连接｜记录异常并保留原行</p>
                  <div class="form-row">
                    <label>步骤名称</label>
                    <input name="step_{{ step.index }}_name" type="text" value="{{ step.name }}">
                  </div>
                  <div class="form-row">
                    <label>配置表类型</label>
                    <select name="step_{{ step.index }}_rule_table" onchange="this.form.submit()">
                      {% for item in merge_config.rule_table_options %}
                        <option value="{{ item.key }}" {% if item.key == step.right_table %}selected{% endif %}>{{ item.name }}</option>
                      {% endfor %}
                    </select>
                  </div>
                  <h2>匹配字段</h2>
                  <div class="checkbox-grid">
                    {% for item in step.match_options %}
                      <label class="check-item">
                        <input type="checkbox" name="step_{{ step.index }}_match_fields" value="{{ item.field }}" {% if item.field in step.selected_fields %}checked{% endif %}>
                        <span>{{ item.label }}</span>
                      </label>
                    {% endfor %}
                  </div>
                  <h2 style="margin-top:16px">输出字段</h2>
                  <div class="checkbox-grid">
                    {% for item in step.output_options %}
                      <label class="check-item">
                        <input type="checkbox" name="step_{{ step.index }}_output_fields" value="{{ item.field }}" {% if item.field in step.selected_output_fields %}checked{% endif %}>
                        <span>{{ item.label }}</span>
                      </label>
                    {% else %}
                      <span class="muted">当前配置表暂无可选择输出字段，请先上传配置表数据。</span>
                    {% endfor %}
                  </div>
                </div>
              {% endfor %}
              <div class="step-card" id="calculated-columns">
                <div class="button-row" style="margin-top:0">
                  <div>
                    <h2 style="margin:0">新增计算列</h2>
                    <p class="muted" style="margin:6px 0 0">按顺序执行，后一列可以引用前一列。公式以字段编码为主，也支持 [中文字段名]。</p>
                  </div>
                  <button class="button secondary" id="add-calc-column" type="button">新增计算列</button>
                </div>
                <div id="calc-column-list" class="calc-column-list">
                  {% for col in flow_config.calculated_columns %}
                    <div class="calc-column-row">
                      <input type="hidden" name="calc_index" value="{{ col.index }}">
                      <label class="check-item" style="min-height:38px"><input type="checkbox" name="calc_{{ col.index }}_active" value="Y" {% if col.active %}checked{% endif %}><span>启用</span></label>
                      <input name="calc_{{ col.index }}_name" type="text" value="{{ col.name }}" placeholder="列名，例如 加工费CNY">
                      <input name="calc_{{ col.index }}_field" type="text" value="{{ col.field }}" placeholder="字段编码，例如 fee_amount_cny">
                      <input name="calc_{{ col.index }}_formula" type="text" value="{{ col.formula }}" placeholder="公式，例如 num(pnl_delivery_qty) * num(unit_fee_cny)">
                      <button class="button secondary remove-calc-column" type="button">删除</button>
                    </div>
                  {% endfor %}
                </div>
                <template id="calc-column-template">
                  <div class="calc-column-row">
                    <input type="hidden" name="calc_index" value="__INDEX__">
                    <label class="check-item" style="min-height:38px"><input type="checkbox" name="calc___INDEX___active" value="Y" checked><span>启用</span></label>
                    <input name="calc___INDEX___name" type="text" placeholder="列名，例如 新计算列">
                    <input name="calc___INDEX___field" type="text" placeholder="字段编码，例如 custom_amount">
                    <input name="calc___INDEX___formula" type="text" placeholder="公式，例如 num(delivery_qty) * 1.2">
                    <button class="button secondary remove-calc-column" type="button">删除</button>
                  </div>
                </template>
                <p class="muted">可用函数：num()、ifelse()、if_eq()、if_in()、coalesce()、round()。示例：if_eq(currency_code, "CNY", num(unit_fee_local), num(unit_fee_local) * num(exchange_rate))</p>
              </div>
              <div class="actions">
                <button type="submit">保存流程配置</button>
              </div>
            </section>
          </div>
        </form>
        <script>
          (() => {
            const list = document.getElementById('calc-column-list');
            const template = document.getElementById('calc-column-template');
            const addButton = document.getElementById('add-calc-column');
            let nextIndex = {{ (flow_config.calculated_columns|length) + 1 }};
            const bindRemove = (root) => {
              root.querySelectorAll('.remove-calc-column').forEach((button) => {
                button.addEventListener('click', () => button.closest('.calc-column-row')?.remove());
              });
            };
            bindRemove(document);
            addButton?.addEventListener('click', () => {
              const html = template.innerHTML.replaceAll('__INDEX__', String(nextIndex));
              const wrapper = document.createElement('div');
              wrapper.innerHTML = html.trim();
              const row = wrapper.firstElementChild;
              list.appendChild(row);
              bindRemove(row);
              nextIndex += 1;
            });
          })();
        </script>
        {% else %}
          <section class="panel">
            <h2>暂无计算流程配置</h2>
            <p>历史流程已全部删除。可以点击下方按钮重新新增流程。</p>
            <div class="button-row">
              <a class="button" href="{{ url_for('rules', tab='flows', new_flow='1', period=selected_period) }}">新增流程</a>
            </div>
          </section>
        {% endif %}
        {% if show_new_flow %}
          <div class="drawer-backdrop">
            <section class="drawer">
              <div class="drawer-head">
                <div>
                  <h1>新增匹配流程</h1>
                  <p>先创建流程基本信息，可以一次添加多个初始步骤，保存后继续细调。</p>
                </div>
                <a class="button secondary" href="{{ url_for('rules', tab='flows') }}">关闭</a>
              </div>
              <div class="step-tabs"><span>1 基本信息</span><span>2 选择主表</span><span>3 添加任意步骤</span></div>
              <form id="new-flow-form" method="post" action="{{ url_for('create_merge_flow') }}">
                <section class="panel">
                  <h2>基本信息</h2>
                  <div class="form-row"><label>流程名称</label><input name="name" type="text" placeholder="例如 加工费月度计算流程"></div>
                  <div class="form-row"><label>流程编码</label><input name="flow_key" type="text" placeholder="例如 monthly_factory_fee_flow"></div>
                  <div class="form-row"><label>说明</label><input name="description" type="text" placeholder="从SAP流水开始匹配规则表并输出结果"></div>
                </section>
                <section class="panel">
                  <h2>选择主表</h2>
                  <div class="form-row"><label>主表</label><select name="main_table"><option value="raw_sap_monthly_data">表1 SAP流水</option></select></div>
                  <p>批次来源：运行时选择 SAP 批次</p>
                </section>
                <section class="panel">
                  <h2>添加第一步匹配</h2>
                  <div class="form-row"><label>步骤名称</label><input name="step_name" type="text" value="匹配物料主数据"></div>
                  <div class="form-row">
                    <label>匹配规则表</label>
                    <select name="right_table">
                      {% for item in merge_config.rule_table_options %}
                        <option value="{{ item.key }}">{{ item.name }}</option>
                      {% endfor %}
                    </select>
                  </div>
                  <p>合并方式：左连接｜未匹配处理：记录异常并保留原行</p>
                  <div class="checkbox-grid">
                    {% for item in merge_config.field_options %}
                      <label class="check-item">
                        <input type="checkbox" name="match_fields" value="{{ item.field }}" {% if item.field in ['material_code', 'factory_code'] %}checked{% endif %}>
                        <span>{{ item.label }} = {{ item.label }}</span>
                      </label>
                    {% endfor %}
                  </div>
                  <div class="checkbox-grid">
                    {% for item in merge_config.output_options %}
                      <label class="check-item"><input type="checkbox" name="output_fields" value="{{ item.field }}" checked><span>{{ item.label }}</span></label>
                    {% endfor %}
                  </div>
                </section>
                <div id="optional-steps"></div>
                <section class="panel">
                  <div class="button-row" style="margin-top:0">
                    <h2 style="margin:0">更多匹配步骤</h2>
                    <button type="button" id="add-flow-step">新增一步</button>
                  </div>
                  <p>新增后先启用该步骤，字段配置才会展开。步骤数量不设上限。</p>
                </section>
                <template id="optional-step-template">
                  <section class="panel optional-step-card">
                    <div class="button-row" style="margin-top:0">
                      <h2 style="margin:0">可选：添加第__INDEX__步匹配</h2>
                      <label class="check-item" style="min-height:38px"><input class="optional-step-toggle" type="checkbox" name="step___INDEX___enabled" value="Y"><span>启用这一步</span></label>
                      <button class="button secondary remove-flow-step" type="button">删除这一步</button>
                    </div>
                    <div class="optional-step" hidden>
                      <div class="form-row"><label>步骤名称</label><input name="step___INDEX___name" type="text" value="匹配步骤__INDEX__"></div>
                      <div class="form-row">
                        <label>匹配规则表</label>
                        <select name="step___INDEX___right_table">
                          {% for item in merge_config.rule_table_options %}
                            <option value="{{ item.key }}">{{ item.name }}</option>
                          {% endfor %}
                        </select>
                      </div>
                      <h2>匹配字段</h2>
                      <div class="checkbox-grid">
                        {% for item in merge_config.field_options %}
                          <label class="check-item">
                            <input type="checkbox" name="step___INDEX___match_fields" value="{{ item.field }}" {% if item.field in ['material_code', 'factory_code'] %}checked{% endif %}>
                            <span>{{ item.label }} = {{ item.label }}</span>
                          </label>
                        {% endfor %}
                      </div>
                      <h2 style="margin-top:16px">输出字段</h2>
                      <div class="checkbox-grid">
                        {% for item in merge_config.output_options %}
                          <label class="check-item"><input type="checkbox" name="step___INDEX___output_fields" value="{{ item.field }}"><span>{{ item.label }}</span></label>
                        {% endfor %}
                      </div>
                    </div>
                  </section>
                </template>
                <div class="actions">
                  <a class="button secondary" href="{{ url_for('rules', tab='flows') }}">取消</a>
                  <button type="submit">保存并进入配置</button>
                </div>
              </form>
              <script>
                (() => {
                  const addButton = document.getElementById('add-flow-step');
                  const container = document.getElementById('optional-steps');
                  const template = document.getElementById('optional-step-template');
                  let nextIndex = 2;
                  const bindStep = (card) => {
                    const toggle = card.querySelector('.optional-step-toggle');
                    const body = card.querySelector('.optional-step');
                    const remove = card.querySelector('.remove-flow-step');
                    toggle.addEventListener('change', () => {
                      body.hidden = !toggle.checked;
                    });
                    remove.addEventListener('click', () => card.remove());
                  };
                  addButton?.addEventListener('click', () => {
                    const html = template.innerHTML.replaceAll('__INDEX__', String(nextIndex));
                    const wrapper = document.createElement('div');
                    wrapper.innerHTML = html.trim();
                    const card = wrapper.firstElementChild;
                    container.appendChild(card);
                    bindStep(card);
                    nextIndex += 1;
                  });
                })();
              </script>
            </section>
          </div>
        {% endif %}
      {% endif %}
    {% elif page == 'runs' %}
      <h1>运行记录</h1>
      <p>查看历史合并和计算日志，并下载对应的结果与异常 Excel。</p>
      <section class="panel">
        <h2>合并记录</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>合并运行ID</th><th>流程</th><th>SAP导入批次</th><th>输入行数</th><th>输出行数</th><th>异常行数</th><th>状态</th><th>开始时间</th><th>完成时间</th><th>文件</th></tr></thead>
            <tbody>
              {% for row in merge_runs %}
                <tr>
                  <td><a href="{{ url_for('merge_run_detail', merge_run_id=row.merge_run_id) }}">{{ row.merge_run_id }}</a></td><td>{{ row.flow_name }}</td><td>{{ row.import_batch_id }}</td><td>{{ row.left_rows }}</td><td>{{ row.merged_rows }}</td><td>{{ row.exception_rows }}</td><td>{{ row.status_label }}</td><td>{{ row.started_at }}</td><td>{{ row.finished_at }}</td>
                  <td>
                    {% if row.result_file_name %}<a href="{{ url_for('output_file', filename=row.result_file_name) }}">结果</a>{% endif %}
                    {% if row.exception_file_name %}<a href="{{ url_for('output_file', filename=row.exception_file_name) }}">异常</a>{% endif %}
                  </td>
                </tr>
              {% else %}
                <tr><td colspan="10" class="muted">暂无合并记录</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>计算记录</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>计算运行ID</th><th>SAP导入批次</th><th>SAP 文件名</th><th>SAP 导入行数</th><th>计算结果行数</th><th>异常行数</th><th>年月</th><th>工厂</th><th>状态</th><th>开始时间</th><th>完成时间</th><th>文件</th></tr></thead>
            <tbody>
              {% for row in runs %}
                <tr>
                  <td>{{ row.run_id }}</td><td>{{ row.import_batch_id }}</td><td>{{ row.sap_file_name }}</td><td>{{ row.sap_import_rows }}</td><td>{{ row.calc_result_rows }}</td><td>{{ row.calc_exception_rows }}</td><td>{{ row.yyyymm }}</td><td>{{ row.factory_scope }}</td><td>{{ row.status_label }}</td><td>{{ row.started_at }}</td><td>{{ row.finished_at }}</td>
                  <td>
                    {% if row.result_file %}<a href="{{ url_for('output_file', filename=row.result_file) }}">结果</a>{% endif %}
                    {% if row.exception_file %}<a href="{{ url_for('output_file', filename=row.exception_file) }}">异常</a>{% endif %}
                  </td>
                </tr>
              {% else %}
                <tr><td colspan="12" class="muted">暂无运行记录</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </section>
    {% elif page == 'results' %}
      <h1>计算结果</h1>
      <p>直接在页面查看匹配流程输出结果；这里默认展示最近一次合并，也可以切换历史批次。</p>
      <section class="panel">
        <h2>选择结果批次</h2>
        <form method="get" action="{{ url_for('results') }}">
          <div class="form-row">
            <label for="result_merge_run_id">合并运行</label>
            <select id="result_merge_run_id" name="merge_run_id">
              {% for row in merge_runs %}
                <option value="{{ row.merge_run_id }}" {% if row.merge_run_id == selected_run_id %}selected{% endif %}>{{ row.flow_name }}｜{{ row.merge_run_id }}｜{{ row.merged_rows }} 行｜{{ row.status_label }}</option>
              {% else %}
                <option value="">暂无可查看的计算结果</option>
              {% endfor %}
            </select>
          </div>
          <div class="actions">
            <button type="submit">查看结果</button>
            <a class="button secondary" href="{{ url_for('rules', tab='flows') }}">运行匹配流程</a>
            {% if selected_run_id %}
              <button class="button secondary" type="submit" form="delete-current-result">删除当前结果</button>
            {% endif %}
          </div>
        </form>
        {% if selected_run_id %}
          <form id="delete-current-result" method="post" action="{{ url_for('delete_merge_run', merge_run_id=selected_run_id, period=selected_period) }}"></form>
        {% endif %}
      </section>
      {% if detail.exists %}
        <section class="panel">
          <h2>结果概览</h2>
          <div class="grid">
            <div class="metric"><span class="muted">输入行数</span><strong>{{ detail.run.left_rows }}</strong></div>
            <div class="metric"><span class="muted">结果行数</span><strong>{{ detail.run.merged_rows }}</strong></div>
            <div class="metric"><span class="muted">异常行数</span><strong>{{ detail.run.exception_rows }}</strong></div>
          </div>
          <p>{{ detail.run.flow_name }}｜SAP导入批次：{{ detail.run.import_batch_id }}｜状态：{{ detail.run.status_label }}</p>
          <p>配置表版本：{{ detail.run.config_versions_label or '未记录' }}</p>
        </section>
        <section class="panel">
          <h2>计算结果明细</h2>
          <p>共 {{ detail.result_preview.row_count }} 行，页面预览前 50 行。</p>
          <div class="table-wrap dense">
            <table>
              <thead><tr>{% for col in detail.result_preview.columns %}<th>{{ col }}</th>{% endfor %}</tr></thead>
              <tbody>
                {% for row in detail.result_preview.rows %}
                  <tr>{% for col in detail.result_preview.columns %}<td>{{ row[col] }}</td>{% endfor %}</tr>
                {% else %}
                  <tr><td class="muted">暂无计算结果</td></tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </section>
        <section class="panel">
          <h2>异常明细</h2>
          <p>共 {{ detail.exception_preview.row_count }} 行，页面预览前 50 行。</p>
          <div class="table-wrap dense">
            <table>
              <thead><tr>{% for col in detail.exception_preview.columns %}<th>{{ col }}</th>{% endfor %}</tr></thead>
              <tbody>
                {% for row in detail.exception_preview.rows %}
                  <tr>{% for col in detail.exception_preview.columns %}<td>{{ row[col] }}</td>{% endfor %}</tr>
                {% else %}
                  <tr><td class="muted">暂无异常</td></tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </section>
      {% else %}
        <section class="panel">
          <h2>暂无计算结果</h2>
          <p>请先在“匹配流程管理”里运行一次流程，运行完成后这里会自动展示。</p>
        </section>
      {% endif %}
    {% elif page == 'exports' %}
      <h1>加工费导出</h1>
      <p>面向最终使用者展示加工费结果。这里可以从计算结果中勾选需要的字段，并把导出版本留存在历史记录中。</p>
      <section class="panel">
        <h2>选择计算结果</h2>
        <form method="get" action="{{ url_for('exports') }}">
          <div class="form-row">
            <label for="export_merge_run_id">计算结果批次</label>
            <select id="export_merge_run_id" name="merge_run_id">
              {% for row in merge_runs %}
                <option value="{{ row.merge_run_id }}" {% if row.merge_run_id == selected_run_id %}selected{% endif %}>{{ row.flow_name }}｜{{ row.merge_run_id }}｜{{ row.merged_rows }} 行｜{{ row.status_label }}</option>
              {% else %}
                <option value="">暂无可导出的计算结果</option>
              {% endfor %}
            </select>
          </div>
          <div class="actions">
            <button type="submit">切换结果</button>
          </div>
        </form>
      </section>
      <section class="panel">
        <h2>选择导出字段</h2>
        {% if selected_run_id and export_fields %}
        <form id="export-fields-form" method="get" action="{{ url_for('exports') }}">
          <input type="hidden" name="merge_run_id" value="{{ selected_run_id }}">
          <input type="hidden" name="field_selection" value="1">
          <div class="checkbox-grid">
            {% for item in export_fields %}
              <label class="check-item">
                <input type="checkbox" name="export_fields" value="{{ item.field }}" {% if item.field in selected_export_fields %}checked{% endif %} onchange="this.form.submit()">
                {{ item.label }}
              </label>
            {% endfor %}
          </div>
          <div class="actions">
            <button type="submit" formmethod="post" formaction="{{ url_for('create_export') }}">生成导出版本</button>
            <a class="button secondary" href="{{ url_for('results', merge_run_id=selected_run_id) }}">查看回溯明细</a>
          </div>
        </form>
        {% else %}
          <p>暂无字段可选。请先完成一次计算，系统会根据计算结果自动识别可导出字段。</p>
        {% endif %}
      </section>
      <section class="panel">
        <h2>导出预览</h2>
        <p>共 {{ export_preview.row_count }} 行，页面预览前 50 行。</p>
        <div class="table-wrap dense">
          <table>
            <thead><tr>{% for col in export_preview.columns %}<th>{{ col }}</th>{% endfor %}</tr></thead>
            <tbody>
              {% for row in export_preview.rows %}
                <tr>{% for col in export_preview.columns %}<td>{{ row[col] }}</td>{% endfor %}</tr>
              {% else %}
                <tr><td class="muted">暂无可预览数据</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>导出历史</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>导出ID</th><th>归属年月</th><th>计算结果批次</th><th>流程</th><th>SAP批次</th><th>行数</th><th>字段数</th><th>生成时间</th><th>文件</th></tr></thead>
            <tbody>
              {% for row in export_history %}
                <tr>
                  <td>{{ row.export_id }}</td>
                  <td>{{ row.period }}</td>
                  <td>{{ row.merge_run_id }}</td>
                  <td>{{ row.flow_name }}</td>
                  <td>{{ row.import_batch_id }}</td>
                  <td>{{ row.row_count }}</td>
                  <td>{{ row.field_count }}</td>
                  <td>{{ row.created_at }}</td>
                  <td><a href="{{ url_for('download_export', period=row.period, filename=row.file_name) }}">下载</a></td>
                </tr>
              {% else %}
                <tr><td colspan="9" class="muted">暂无导出历史</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </section>
    {% elif page == 'help' %}
      <h1>使用指引与Q&A</h1>
      <p>按顶部菜单从左到右完成日常操作：先准备数据和规则，再执行计算，最后生成面向业务用户的加工费导出。</p>
      <section class="panel">
        <h2>1.0 推荐操作流程</h2>
        <div class="grid">
          <div class="metric"><span class="muted">第一步</span><strong>事实表管理</strong><p>上传表1 SAP流水，系统生成 SAP 导入批次，并保留原始明细。</p></div>
          <div class="metric"><span class="muted">第二步</span><strong>配置表管理</strong><p>维护表2、表3、表4、表5以及新增配置表类型；每张表可以保留多个版本。</p></div>
          <div class="metric"><span class="muted">第三步</span><strong>计算流程设置</strong><p>用流程图定义串型匹配步骤：选择配置表类型、匹配字段、输出字段。</p></div>
          <div class="metric"><span class="muted">第四步</span><strong>执行计算</strong><p>选择 SAP 批次、计算流程，并为每个步骤选择配置表版本后运行。</p></div>
          <div class="metric"><span class="muted">第五步</span><strong>计算结果</strong><p>查看完整回溯明细、异常信息和每一步命中情况。</p></div>
          <div class="metric"><span class="muted">第六步</span><strong>加工费导出</strong><p>勾选给业务用户看的字段，生成可留存、可下载的加工费导出版本。</p></div>
        </div>
      </section>
      <section class="panel">
        <h2>页面怎么用</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>页面</th><th>主要用途</th><th>注意事项</th></tr></thead>
            <tbody>
              <tr><td>1 事实表管理</td><td>导入 SAP 流水，查看导入批次和数据预览。</td><td>导入前不做强字段校验，字段会尽量按原始表头保留。</td></tr>
              <tr><td>2 配置表管理</td><td>管理配置表类型和配置表版本，上传、启停、删除、下载版本。</td><td>新增字段允许保留在后续列；页面展示优先使用中文字段名。</td></tr>
              <tr><td>3 计算流程设置</td><td>维护可视化匹配流程，流程只绑定配置表类型，不绑定具体版本。</td><td>新增步骤统一通过流程图里的“新增节点”完成。</td></tr>
              <tr><td>4 执行计算</td><td>选择 SAP 批次、流程和各步骤版本，正式生成计算结果。</td><td>运行前检查“本次计算清单”，确认版本组合正确。</td></tr>
              <tr><td>5 计算结果</td><td>查看回溯明细、异常和配置版本来源。</td><td>这里是完整追溯表，字段较多，适合排查和复核。</td></tr>
              <tr><td>6 加工费导出</td><td>勾选字段，生成面向业务用户的结果表。</td><td>导出预览会随字段勾选同步变化；历史导出会留存。</td></tr>
              <tr><td>日志</td><td>查看导入、计算、合并、异常等历史记录。</td><td>适合追溯某次运行是否成功，以及下载历史结果文件。</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>数据与规则</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>问题</th><th>回答</th></tr></thead>
            <tbody>
              <tr><td>SAP事实表和配置表有什么区别？</td><td>SAP事实表是每次计算的业务流水；配置表是主数据、系数、单价、损益规则等可复用版本。</td></tr>
              <tr><td>匹配流程是串型的吗？</td><td>是。表1先匹配表2，表2带出的机型、工厂物料组等字段可以继续参与表3、表4、表5匹配。</td></tr>
              <tr><td>匹配字段从哪里来？</td><td>系统会根据上一步结果和当前配置表版本自动识别可匹配字段，只展示两边都存在的字段。</td></tr>
              <tr><td>输出字段从哪里来？</td><td>输出字段来自当前配置表中未作为匹配字段的字段，目的是把规则表里的业务属性带入结果。</td></tr>
              <tr><td>为什么某张表有数据但匹配不到？</td><td>常见原因是匹配字段值不一致、版本选错、字段表头不一致、空值、是否启用或优先级不符合当前流程。</td></tr>
              <tr><td>规则表允许新增字段吗？</td><td>允许。标准字段会排在前面，额外字段会保留在后续列。</td></tr>
              <tr><td>页面字段为什么都是中文？</td><td>系统展示层会把内部字段名转换成中文；后续可以扩展成中英文双语。</td></tr>
              <tr><td>删除配置表版本会删除什么？</td><td>删除的是当前选中的配置表版本文件和版本记录，不会删除其他版本。</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>结果与导出</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>问题</th><th>回答</th></tr></thead>
            <tbody>
              <tr><td>计算结果在哪里看？</td><td>进入“5 计算结果”，选择合并运行批次，即可直接在页面查看，不需要先下载。</td></tr>
              <tr><td>为什么计算结果表很宽？</td><td>计算结果是回溯表，会保留规则字段、命中状态、异常原因等信息。给业务用户看的版本建议从“6 加工费导出”生成。</td></tr>
              <tr><td>加工费导出和计算结果有什么区别？</td><td>计算结果用于复核和追溯；加工费导出用于交付，可以只勾选用户需要看的字段。</td></tr>
              <tr><td>导出字段能调整吗？</td><td>可以。勾选字段后，导出预览会立即同步，生成导出版本时使用当前勾选字段。</td></tr>
              <tr><td>导出历史会保留吗？</td><td>会。系统会记录导出ID、归属年月、计算结果批次、字段数、行数、生成时间和文件。</td></tr>
              <tr><td>删除SAP批次会影响什么？</td><td>会同步删除该批次的原始SAP行、相关合并结果、异常和计算记录。</td></tr>
              <tr><td>默认匹配流程能删除吗？</td><td>可以。删除全部历史流程后，计算流程设置页会显示空状态，并保留新增流程入口。</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>上线使用说明</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>问题</th><th>回答</th></tr></thead>
            <tbody>
              <tr><td>当前本地版适合什么场景？</td><td>适合单机使用、现场演示、规则验证和小范围试运行。数据保存在本机 SQLite 和文件目录中。</td></tr>
              <tr><td>多人在线使用需要补什么？</td><td>需要登录权限、云端数据库、文件对象存储、备份恢复、操作审计和并发控制。</td></tr>
              <tr><td>Netlify可以做什么？</td><td>Netlify适合承载网页前端、公开访问入口和轻量接口。完整生产化需要把当前本地存储迁移到云端存储或外部数据库。</td></tr>
              <tr><td>为什么不能只把本地Flask直接放上去？</td><td>当前系统依赖本地 SQLite 和本地文件写入；Netlify的无服务器运行环境不适合直接承载这种长期可写的本地状态。</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    {% elif page == 'merge_detail' %}
      {% if detail.exists %}
        <h1>合并结果</h1>
        <p>{{ detail.run.flow_name }}｜{{ detail.run.merge_run_id }}</p>
        <section class="panel">
          <h2>运行信息</h2>
          <div class="grid">
            <div class="metric"><span class="muted">输入行数</span><strong>{{ detail.run.left_rows }}</strong></div>
            <div class="metric"><span class="muted">输出行数</span><strong>{{ detail.run.merged_rows }}</strong></div>
            <div class="metric"><span class="muted">异常行数</span><strong>{{ detail.run.exception_rows }}</strong></div>
          </div>
          <div class="actions">
            {% if detail.run.result_file_name %}<a class="button" href="{{ url_for('output_file', filename=detail.run.result_file_name) }}">下载合并结果</a>{% endif %}
            {% if detail.run.exception_file_name %}<a class="button secondary" href="{{ url_for('output_file', filename=detail.run.exception_file_name) }}">下载异常明细</a>{% endif %}
            <a class="button secondary" href="{{ url_for('runs') }}">返回记录</a>
          </div>
        </section>
        <section class="panel">
          <h2>合并结果预览</h2>
          <p>共 {{ detail.result_preview.row_count }} 行，预览前 50 行。</p>
          <div class="table-wrap">
            <table>
              <thead><tr>{% for col in detail.result_preview.columns %}<th>{{ col }}</th>{% endfor %}</tr></thead>
              <tbody>
                {% for row in detail.result_preview.rows %}
                  <tr>{% for col in detail.result_preview.columns %}<td>{{ row[col] }}</td>{% endfor %}</tr>
                {% else %}
                  <tr><td class="muted">暂无合并结果</td></tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </section>
        <section class="panel">
          <h2>异常预览</h2>
          <p>共 {{ detail.exception_preview.row_count }} 行，预览前 50 行。</p>
          <div class="table-wrap">
            <table>
              <thead><tr>{% for col in detail.exception_preview.columns %}<th>{{ col }}</th>{% endfor %}</tr></thead>
              <tbody>
                {% for row in detail.exception_preview.rows %}
                  <tr>{% for col in detail.exception_preview.columns %}<td>{{ row[col] }}</td>{% endfor %}</tr>
                {% else %}
                  <tr><td class="muted">暂无异常</td></tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </section>
      {% else %}
        <h1>合并结果不存在</h1>
        <p>未找到合并运行：{{ detail.merge_run_id }}</p>
      {% endif %}
    {% endif %}
  </main>
</body>
</html>
"""

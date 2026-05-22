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
        return redirect(url_for("periods_page"))

    @app.get("/periods")
    def periods_page() -> str:
        cfg = _cfg(app)
        period_id = _selected_period_id(cfg)
        return render_template_string(
            BASE_TEMPLATE,
            page="periods",
            cfg=replace(cfg, yyyymm=period_id),
            rule_tables=_rule_tables(cfg),
            periods=_periods(cfg),
            selected_period=period_id,
            period_context=_period_management_context(cfg, period_id),
        )

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
        selected_batch = request.args.get("import_batch_id", "").strip()
        preview = _sap_batch_preview(cfg, selected_batch, limit=100) if selected_batch else {"columns": [], "rows": [], "row_count": 0}
        return render_template_string(
            BASE_TEMPLATE,
            page="sap",
            cfg=replace(cfg, yyyymm=period_id),
            rule_tables=_rule_tables(cfg),
            batches=batches,
            selected_batch=selected_batch,
            selected_batch_meta=_sap_batch_meta(batches, selected_batch),
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

    @app.get("/sap/batches/<import_batch_id>/download")
    def download_sap_batch(import_batch_id: str) -> Response:
        cfg = _cfg(app)
        df = _sap_batch_export_df(cfg, import_batch_id)
        if df.empty:
            flash("当前 SAP 批次没有可下载数据。", "error")
            return redirect(url_for("sap_data"))
        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="SAP流水"[:31])
        output.seek(0)
        return send_file(
            output,
            as_attachment=True,
            download_name=f"{secure_filename(import_batch_id)}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

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
        show_new_type = request.args.get("new_type") == "1"
        show_new_version = request.args.get("new_version") == "1"
        show_table_preview = request.args.get("preview") == "1"
        edit_step_index = _safe_int(request.args.get("edit_step"), 0)
        selected_step_index = _safe_int(request.args.get("selected_step"), edit_step_index)
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
            preview = _table_preview(preview_path, selected, limit=100)
        merge_config = _merge_material_config(replace(cfg, yyyymm=period_id))
        merge_flows = _merge_flows_summary(cfg)
        selected_flow_key = request.args.get("flow") or (merge_flows[0]["key"] if merge_flows else "")
        flow_config = _merge_flow_config(cfg, selected_flow_key)
        valid_step_indexes = [step["index"] for step in flow_config.get("steps", [])]
        if edit_step_index and edit_step_index not in valid_step_indexes:
            edit_step_index = 0
        if selected_step_index not in valid_step_indexes:
            selected_step_index = edit_step_index if edit_step_index in valid_step_indexes else (valid_step_indexes[0] if valid_step_indexes else 0)
        execution_config = _execution_config(cfg, selected_flow_key)
        batches = _sap_batches(cfg)
        return render_template_string(
            BASE_TEMPLATE,
            page="rules",
            rules_tab=rules_tab,
            show_new_flow=show_new_flow,
            show_new_type=show_new_type,
            show_new_version=show_new_version,
            show_table_preview=show_table_preview,
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
            edit_step_index=edit_step_index,
            selected_step_index=selected_step_index,
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
        return redirect(url_for("rules", tab="flows", flow=flow_key, selected_step=1))

    @app.post("/merge/flows/<flow_key>/update")
    def update_merge_flow(flow_key: str) -> Response:
        cfg = _cfg(app)
        flow_key = _safe_table_key(flow_key)
        rule_tables = _rule_tables(cfg)
        step_indexes = sorted({int(value) for value in request.form.getlist("step_index") if str(value).isdigit()})
        steps = []
        left_fields = _sap_fact_fields()
        for step_index in step_indexes:
            step_category = _normalize_step_category(request.form.get(f"step_{step_index}_category", "add_field"))
            step_action = _normalize_step_action(request.form.get(f"step_{step_index}_action", "merge"))
            if step_category == "modify_field":
                step_action = "calculate"
            step_name = request.form.get(f"step_{step_index}_name", f"步骤{step_index}").strip() or f"步骤{step_index}"
            if step_action == "calculate":
                steps.append({
                    "name": step_name,
                    "step_category": step_category,
                    "step_action": step_action,
                    "calculated_columns": _calculated_columns_from_form(request.form, prefix=f"step_{step_index}_calc"),
                })
                left_fields = _merge_unique_fields(left_fields, [
                    col["field"]
                    for col in steps[-1]["calculated_columns"]
                    if step_category == "add_field"
                ])
                continue
            default_rule_table = next(iter(rule_tables), "")
            rule_table = request.form.get(f"step_{step_index}_rule_table", default_rule_table).strip() or default_rule_table
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
                "name": step_name,
                "step_category": step_category,
                "step_action": step_action,
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
        selected_step = _safe_int(request.form.get("selected_step"), 0)
        return_edit_step = _safe_int(request.form.get("return_edit_step"), 0)
        redirect_args: dict[str, Any] = {
            "tab": "flows",
            "flow": flow_key,
            "selected_step": selected_step or 1,
            "period": _selected_period_id(cfg),
        }
        if return_edit_step:
            redirect_args["edit_step"] = return_edit_step
        return redirect(url_for("rules", **redirect_args))

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
            "step_category": "add_field",
            "step_action": "merge",
            "left_table": "previous_result" if steps else "raw_sap_monthly_data",
            "right_table": default_table,
            "join_type": "left",
            "unmatched": "mark_exception",
            "keys": [{"left": "material_code", "right": "material_code"}],
            "output_fields": [],
        })
        _save_merge_config(cfg, raw)
        flash("步骤已新增，请在步骤详情中调整配置。", "success")
        new_step_index = len(steps)
        return redirect(url_for(
            "rules",
            tab="flows",
            flow=flow_key,
            selected_step=new_step_index,
            edit_step=new_step_index,
            period=_selected_period_id(cfg),
        ))

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
        selected_step = min(step_index, len(steps)) if steps else 0
        return redirect(url_for("rules", tab="flows", flow=flow_key, selected_step=selected_step, period=_selected_period_id(cfg)))

    @app.post("/merge/flows/<flow_key>/steps/<int:step_index>/move/<direction>")
    def move_merge_flow_step(flow_key: str, step_index: int, direction: str) -> Response:
        cfg = _cfg(app)
        flow_key = _safe_table_key(flow_key)
        raw = _load_merge_config(cfg)
        flow = raw.setdefault("flows", {}).setdefault(flow_key, {})
        steps = list(flow.get("steps", []))
        current = step_index - 1
        target = current - 1 if direction == "up" else current + 1
        if 0 <= current < len(steps) and 0 <= target < len(steps):
            steps[current], steps[target] = steps[target], steps[current]
            flow["steps"] = steps
            _save_merge_config(cfg, raw)
            flash("步骤顺序已更新。", "success")
        else:
            flash("当前步骤无法继续移动。", "error")
        selected_step = target + 1 if 0 <= target < len(steps) else step_index
        return redirect(url_for("rules", tab="flows", flow=flow_key, selected_step=selected_step, period=_selected_period_id(cfg)))

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
        return redirect(url_for("periods_page", period=period_id))

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


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


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


def _config_versions_count_label(raw_json: str) -> str:
    try:
        mapping = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        mapping = {}
    count = len([value for value in mapping.values() if value])
    return f"{count} 个版本" if count else "未记录"


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


def _period_management_context(cfg: AppConfig, selected_period: str) -> dict[str, Any]:
    selected_period = normalize_yyyymm(selected_period)
    periods = _periods(cfg)
    current = next((row for row in periods if str(row.get("period_id")) == selected_period), None)
    if not current:
        current = {"period_id": selected_period, "status": "DRAFT", "status_label": "草稿", "created_at": "", "closed_at": "", "archived_at": "", "message": ""}

    latest_sap: dict[str, dict[str, Any]] = {}
    latest_merge: dict[str, dict[str, Any]] = {}
    conn = _connect_for_read(cfg)
    try:
        sap_rows = pd.read_sql_query(
            """
            SELECT COALESCE(period_id, yyyymm) AS period_id, import_batch_id, source_file_name, import_rows, imported_at
            FROM sap_import_batch
            WHERE COALESCE(period_id, yyyymm) <> ''
            ORDER BY imported_at DESC
            """,
            conn,
        ).to_dict("records")
        for row in sap_rows:
            latest_sap.setdefault(str(row.get("period_id")), row)

        merge_rows = pd.read_sql_query(
            """
            SELECT COALESCE(period_id, '') AS period_id, merge_run_id, flow_name, merged_rows, exception_rows, status, finished_at, started_at
            FROM merge_run_log
            WHERE COALESCE(period_id, '') <> ''
            ORDER BY COALESCE(finished_at, started_at) DESC
            """,
            conn,
        ).to_dict("records")
        for row in merge_rows:
            latest_merge.setdefault(str(row.get("period_id")), _localize_status_fields(row))
    finally:
        conn.close()

    latest_export: dict[str, dict[str, Any]] = {}
    for row in _export_history(cfg):
        period_id = normalize_yyyymm(str(row.get("period") or ""))
        if period_id:
            latest_export.setdefault(period_id, row)

    rows = []
    for period in periods:
        period_id = str(period.get("period_id"))
        sap = latest_sap.get(period_id, {})
        merge = latest_merge.get(period_id, {})
        export = latest_export.get(period_id, {})
        rows.append({
            **period,
            "fact_batch_label": sap.get("import_batch_id") or "未导入",
            "fact_file_label": sap.get("source_file_name") or "",
            "latest_task_label": merge.get("merge_run_id") or "无",
            "latest_task_status": merge.get("status_label") or "",
            "latest_export_label": export.get("export_id") or "无",
            "note": period.get("message") or "",
            "is_current": period_id == selected_period,
        })

    return {
        "current": current,
        "rows": rows,
        "current_sap": latest_sap.get(selected_period, {}),
        "current_merge": latest_merge.get(selected_period, {}),
        "current_export": latest_export.get(selected_period, {}),
        "is_locked": str(current.get("status")) in {"CLOSED", "ARCHIVED"},
    }


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
    return _period_status(cfg, period_id) in {"CLOSED", "ARCHIVED"}


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
        merge_runs = [
            row for row in _recent_merge_runs(conn, limit=10, period_id=cfg.yyyymm)
            if str(row.get("status") or "") != "RUNNING"
        ][:5]
        for row in merge_runs:
            row["config_versions_summary"] = _config_versions_count_label(str(row.get("config_versions_json") or "{}"))
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


def _sap_batch_meta(batches: list[dict[str, Any]], import_batch_id: str) -> dict[str, Any]:
    for batch in batches:
        if batch.get("import_batch_id") == import_batch_id:
            return batch
    return {}


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


def _sap_batch_export_df(cfg: AppConfig, import_batch_id: str) -> pd.DataFrame:
    conn = _connect_for_read(cfg)
    try:
        raw_rows = conn.execute(
            """
            SELECT source_columns_json, raw_json
            FROM raw_sap_monthly_data
            WHERE import_batch_id = ?
              AND raw_json IS NOT NULL
              AND raw_json <> ''
            ORDER BY raw_id
            """,
            (import_batch_id,),
        ).fetchall()
        if raw_rows:
            columns = _decode_source_columns(raw_rows[0][0])
            rows = []
            for _, raw_json in raw_rows:
                raw = _decode_raw_row(raw_json)
                rows.append({_display_column_label(col): raw.get(col, "") for col in columns})
            return pd.DataFrame(rows, columns=[_display_column_label(col) for col in columns])

        df = pd.read_sql_query(
            """
            SELECT raw_id, yyyymm, factory_code, factory_name, movement_type, location_code, username,
                   order_no, legal_entity_code, biz_date, material_group, material_code, material_desc,
                   delivery_qty_original, delivery_qty, source_table_name, source_file_name, source_row_no
            FROM raw_sap_monthly_data
            WHERE import_batch_id = ?
            ORDER BY raw_id
            """,
            conn,
            params=(import_batch_id,),
        )
    finally:
        conn.close()
    return df.rename(columns={col: SAP_PREVIEW_LABELS.get(col, col) for col in df.columns})


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
    raw_steps = [
        dict(step)
        for step in flow.get("steps", [])
        if step.get("right_table") or step.get("step_action") or step.get("operation")
    ]
    if not raw_steps:
        priority_specs = flow.get("priority_specs", [])
        keys = priority_specs[0].get("keys", []) if priority_specs else []
        raw_steps = [{
            "name": "匹配规则表",
            "step_category": "add_field",
            "step_action": "merge",
            "right_table": flow.get("right_table", "table2_material_master"),
            "keys": keys,
            "output_fields": flow.get("output_fields", []),
            "unmatched": flow.get("unmatched", "mark_exception"),
        }]
    if flow.get("calculated_columns") and not any(_normalize_step_action(step.get("step_action", step.get("operation", ""))) == "calculate" for step in raw_steps):
        raw_steps.append({
            "name": "新增计算字段",
            "step_category": "add_field",
            "step_action": "calculate",
            "calculated_columns": flow.get("calculated_columns", []),
        })
    steps = []
    rule_tables = _rule_tables(cfg)
    left_fields = _sap_fact_fields()
    for index, step in enumerate(raw_steps, start=1):
        step_category = _normalize_step_category(step.get("step_category", step.get("category", "add_field")))
        step_action = _normalize_step_action(step.get("step_action", step.get("operation", "merge")))
        if step_category == "modify_field":
            step_action = "calculate"
        right_table = str(step.get("right_table", "table2_material_master")) if step_action == "merge" else ""
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
        calculated_columns = _calculated_columns_from_config(step.get("calculated_columns", []))
        steps.append({
            "index": index,
            "name": str(step.get("name", f"步骤{index}")),
            "step_category": step_category,
            "step_action": step_action,
            "step_category_label": "新增字段" if step_category == "add_field" else "修改字段",
            "step_action_label": "合并" if step_action == "merge" else "计算",
            "left_table": "raw_sap_monthly_data" if index == 1 else "previous_result",
            "left_source_name": "事实表" if index == 1 else "上一步结果",
            "left_source_detail": "运行时选择 SAP 导入批次" if index == 1 else "承接前一步输出字段",
            "right_table": right_table,
            "right_table_name": rule_tables.get(right_table, {}).get("name", right_table),
            "selected_fields": selected_fields,
            "selected_labels": [match_labels.get(field, _display_column_label(field)) for field in selected_fields],
            "match_options": match_options,
            "selected_output_fields": selected_output_fields,
            "selected_output_labels": [output_labels.get(field, _display_column_label(field)) for field in selected_output_fields],
            "output_options": output_options,
            "calculated_columns": calculated_columns,
            "unmatched": str(step.get("unmatched", "mark_exception")),
        })
        if step_action == "merge":
            left_fields = _merge_unique_fields(left_fields, selected_output_fields)
        elif step_category == "add_field":
            left_fields = _merge_unique_fields(left_fields, [col["field"] for col in calculated_columns])
    return steps


def _flow_calculated_columns_config(flow: dict[str, Any]) -> list[dict[str, Any]]:
    columns = flow.get("calculated_columns", _default_calculated_columns())
    return _calculated_columns_from_config(columns)


def _calculated_columns_from_config(columns: Any) -> list[dict[str, Any]]:
    columns = [dict(item) for item in columns or []]
    out = []
    for index, column in enumerate(columns, start=1):
        out.append({
            "index": index,
            "field": str(column.get("field", "")),
            "name": str(column.get("name", "")),
            "formula": str(column.get("formula", "")),
            "method": _normalize_calc_method(column.get("method", column.get("mode", "formula"))),
            "method_label": "SQL表达式" if _normalize_calc_method(column.get("method", column.get("mode", "formula"))) == "sql" else "公式",
            "active": str(column.get("active", "Y")).upper() in {"Y", "YES", "TRUE", "1", "是"},
        })
    return out


def _calculated_columns_from_form(form: Any, prefix: str = "calc") -> list[dict[str, str]]:
    columns = []
    indexes = sorted({int(value) for value in form.getlist(f"{prefix}_index") if str(value).isdigit()})
    for index in indexes:
        field = _safe_table_key(form.get(f"{prefix}_{index}_field", ""))
        name = form.get(f"{prefix}_{index}_name", "").strip()
        formula = form.get(f"{prefix}_{index}_formula", "").strip()
        if not field or not formula:
            continue
        columns.append({
            "field": field,
            "name": name or _display_column_label(field),
            "formula": formula,
            "method": _normalize_calc_method(form.get(f"{prefix}_{index}_method", "formula")),
            "active": "Y" if form.get(f"{prefix}_{index}_active") == "Y" else "N",
        })
    return columns


def _normalize_step_category(value: object) -> str:
    text = str(value or "").strip()
    return text if text in {"add_field", "modify_field"} else "add_field"


def _normalize_step_action(value: object) -> str:
    text = str(value or "").strip()
    return text if text in {"merge", "calculate"} else "merge"


def _normalize_calc_method(value: object) -> str:
    text = str(value or "").strip().lower()
    return "sql" if text in {"sql", "sql_expr", "sql_expression"} else "formula"


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
    merge_steps = [step for step in steps if step.get("step_action", "merge") == "merge"]
    first_step = merge_steps[0] if merge_steps else steps[0]
    flow["right_table"] = first_step.get("right_table", "")
    flow["join_type"] = "left"
    flow["active_col"] = "is_active"
    flow["priority_specs"] = [{
        "priority": 1,
        "keys": [{"left": field, "right": field} for field in first_step.get("match_fields", [])],
    }]
    flow["output_fields"] = first_step.get("output_fields", [])
    flow["unmatched"] = "mark_exception"
    saved_steps = []
    runtime_calculated_columns: list[dict[str, str]] = []
    for index, step in enumerate(steps, start=1):
        step_action = _normalize_step_action(step.get("step_action", "merge"))
        step_category = _normalize_step_category(step.get("step_category", "add_field"))
        payload = {
            "name": step["name"],
            "step_category": step_category,
            "step_action": step_action,
            "left_table": "raw_sap_monthly_data" if index == 1 else "previous_result",
            "join_type": "left",
            "unmatched": "mark_exception",
        }
        if step_action == "merge":
            payload["right_table"] = step["right_table"]
            payload["keys"] = [{"left": field, "right": field} for field in step["match_fields"]]
            payload["output_fields"] = step["output_fields"]
        else:
            payload["calculated_columns"] = step.get("calculated_columns", [])
            runtime_calculated_columns.extend(payload["calculated_columns"])
        saved_steps.append(payload)
    flow["steps"] = saved_steps
    flow["calculated_columns"] = runtime_calculated_columns or calculated_columns or []
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
        "step_category": "add_field",
        "step_action": "merge",
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
            "step_category": "add_field",
            "step_action": "merge",
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
  <title>工厂加工费计算系统</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #101214;
      --muted: #62666d;
      --line: #e2e5e9;
      --line-strong: #cfd4dc;
      --panel: #ffffff;
      --band: #f7f8f8;
      --soft: #f1f3f4;
      --accent: #111315;
      --accent-dark: #000000;
      --accent-soft: #eef0f1;
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
    header {
      position: sticky;
      top: 0;
      z-index: 10;
      background: rgba(255,255,255,.92);
      color: var(--ink);
      border-bottom: 1px solid var(--line);
      backdrop-filter: saturate(180%) blur(14px);
    }
    .header-inner, main { width: min(1560px, calc(100vw - 48px)); margin: 0 auto; }
    .header-inner { display: flex; align-items: center; justify-content: space-between; min-height: 68px; gap: 24px; }
    .brand { display: grid; gap: 3px; font-size: 18px; font-weight: 700; line-height: 1.15; }
    .brand small { font-size: 12px; font-weight: 500; color: var(--muted); }
    nav { display: flex; gap: 2px; flex-wrap: wrap; justify-content: flex-end; }
    nav a {
      color: var(--muted);
      text-decoration: none;
      padding: 8px 10px;
      border-radius: 999px;
      line-height: 1;
      font-weight: 650;
      font-size: 13px;
    }
    nav a.active, nav a:hover { background: var(--accent-soft); color: var(--ink); }
    main { padding: 34px 0 56px; }
    h1 { font-size: 28px; line-height: 1.15; margin: 0 0 10px; }
    h2 { font-size: 18px; margin: 0 0 14px; }
    p { color: var(--muted); margin: 0; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin: 18px 0; }
    .panel, .metric, .flash {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: none;
    }
    .panel { padding: 22px; margin-top: 18px; overflow: hidden; }
    .metric { padding: 16px; }
    .metric strong { display: block; font-size: 28px; margin-top: 6px; }
    .form-row { display: grid; grid-template-columns: minmax(120px, 180px) minmax(0, 1fr); gap: 12px; align-items: center; margin: 12px 0; }
    label { color: #34373c; font-weight: 650; }
    input[type=text], input[type=file], select {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      padding: 8px 10px;
      background: white;
      color: var(--ink);
    }
    input[type=text]:focus, input[type=file]:focus, select:focus {
      outline: 2px solid #d8dcdf;
      outline-offset: 1px;
      border-color: var(--ink);
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
    .button.secondary { background: white; color: var(--accent); border-color: var(--line-strong); }
    .button.secondary:hover { background: var(--soft); color: var(--ink); }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 14px; }
    .button-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 14px; }
    .button-row form { margin: 0; }
    .inline-form { display: inline-flex; margin: 0; gap: 8px; align-items: center; }
    table { width: max-content; min-width: 100%; border-collapse: collapse; table-layout: auto; }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 9px 10px;
      text-align: left;
      vertical-align: top;
      line-height: 1.35;
      white-space: nowrap;
    }
    th { background: #f4f5f5; color: #34373c; font-weight: 700; position: sticky; top: 0; }
    th, td { white-space: nowrap; min-width: 96px; }
    td { max-width: 420px; overflow: hidden; text-overflow: ellipsis; }
    .table-wrap { overflow: auto; border: 1px solid var(--line); border-radius: 8px; max-height: min(68vh, 720px); }
    .table-wrap.dense th, .table-wrap.dense td { padding: 7px 8px; }
    .muted { color: var(--muted); }
    .flash { padding: 12px 14px; margin: 0 0 12px; }
    .flash.success { border-color: #c8e6d1; background: #f4fbf6; }
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
    .rule-list a.active { border-color: var(--ink); box-shadow: inset 3px 0 0 var(--ink); }
    .rule-list small { display: block; color: var(--muted); margin-top: 5px; line-height: 1.35; }
    .tabs { display: flex; gap: 6px; margin-top: 18px; border-bottom: 1px solid var(--line); }
    .tabs a {
      padding: 10px 14px;
      color: var(--muted);
      text-decoration: none;
      border: 1px solid transparent;
      border-bottom: 0;
      border-radius: 6px 6px 0 0;
      font-weight: 700;
    }
    .tabs a.active { background: white; color: var(--ink); border-color: var(--line); }
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
      min-width: 0;
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
    .stage-column.canvas { background: #fbfbfb; }
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
    .flow-card.active { border-color: var(--ink); box-shadow: inset 3px 0 0 var(--ink); }
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
    .flow-node { display: inline-block; vertical-align: middle; min-width: 220px; max-width: 280px; white-space: normal; border: 1px solid var(--line); border-radius: 8px; background: #fbfbfb; padding: 14px; }
    .diagram-node {
      width: 240px;
      flex: 0 0 240px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
      padding: 14px;
      box-shadow: none;
    }
    .diagram-node.source { border-color: var(--line-strong); background: #f8f9f9; }
    .diagram-node strong { display: block; margin-bottom: 8px; font-size: 15px; }
    .diagram-node span { display: block; color: var(--muted); line-height: 1.45; margin-top: 4px; white-space: normal; }
    .diagram-arrow {
      flex: 0 0 58px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--ink);
      font-size: 24px;
      font-weight: 800;
    }
    .flow-node strong { display: block; margin-bottom: 6px; }
    .flow-node span { display: block; margin-top: 4px; color: var(--muted); }
    .flow-arrow { display: inline-block; vertical-align: middle; padding: 0 8px; color: var(--muted); font-weight: 700; }
    .flow-section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; margin-top: 14px; }
    .flow-section-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
    .flow-section-title { display: grid; gap: 4px; }
    .flow-section-title h2 { margin: 0; }
    .flow-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
    .flow-selector-row { display: grid; grid-template-columns: auto minmax(280px, 520px) minmax(180px, 1fr); gap: 12px; align-items: center; }
    .flow-step-row { cursor: pointer; }
    .flow-step-row.active { background: #f5f5f5; }
    .flow-step-row.active td { font-weight: 650; }
    .flow-step-row:hover { background: #f8f8f8; }
    .step-radio-cell { width: 46px; text-align: center; }
    .step-radio {
      width: 18px;
      height: 18px;
      accent-color: var(--ink);
      cursor: pointer;
      vertical-align: middle;
    }
    .step-diagram-wrap {
      display: grid;
      grid-template-columns: repeat(4, minmax(190px, 1fr));
      column-gap: 44px;
      row-gap: 34px;
      align-items: stretch;
      overflow-x: auto;
      padding: 8px 10px 12px 44px;
    }
    .diagram-step-card {
      position: relative;
      min-height: 142px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 14px;
      box-shadow: none;
    }
    .diagram-step-card.active {
      border-color: var(--ink);
      background: #f4f4f4;
      box-shadow: 0 8px 24px rgba(0,0,0,.08);
    }
    .diagram-step-card strong { display: block; margin-bottom: 8px; font-size: 15px; }
    .diagram-step-card span { display: block; color: var(--muted); line-height: 1.45; margin-top: 4px; }
    .diagram-step-card.has-next::after {
      content: "→";
      position: absolute;
      right: -32px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--ink);
      font-size: 24px;
      font-weight: 800;
    }
    .diagram-step-card.wrap-start::before {
      content: "↳";
      position: absolute;
      left: -34px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--ink);
      font-size: 28px;
      font-weight: 800;
      line-height: 1;
    }
    .summary-list { display: grid; gap: 10px; margin-top: 12px; }
    .summary-item { border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fbfbfb; }
    .version-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin: 12px 0; }
    .version-meta div { border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #fbfbfb; }
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
    .version-card.active { border-color: var(--ink); box-shadow: inset 3px 0 0 var(--ink); }
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
    .modal-backdrop {
      position: fixed;
      inset: 0;
      z-index: 30;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 28px;
      background: rgba(16,18,20,.58);
    }
    .modal {
      width: min(1480px, 86vw);
      height: min(860px, 82vh);
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: 0 24px 90px rgba(0,0,0,.28);
      overflow: hidden;
    }
    .modal-head, .modal-foot {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 18px 22px;
      border-bottom: 1px solid var(--line);
    }
    .modal-foot { border-top: 1px solid var(--line); border-bottom: 0; justify-content: flex-end; }
    .modal-body { padding: 0 22px 18px; min-height: 0; overflow: auto; }
    .modal-summary { display: flex; flex-wrap: wrap; gap: 8px; padding: 14px 22px; }
    .summary-chip { border: 1px solid var(--line); border-radius: 999px; padding: 7px 10px; color: var(--muted); background: #fbfbfb; }
    .modal-table { height: 100%; max-height: none; }
    .modal .calc-column-row { grid-template-columns: 100px minmax(120px, 160px) minmax(140px, 200px) minmax(120px, 150px) minmax(260px, 1fr) auto; }
    .step-tabs { display: flex; gap: 8px; color: var(--muted); font-weight: 700; margin: 12px 0; }
    .step-tabs span { padding: 6px 10px; border: 1px solid var(--line); border-radius: 999px; background: #f8f9f9; }
    .step-tabs span.active { color: var(--ink); border-color: var(--ink); background: #fff; }
    .step-mode-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; align-items: start; }
    .step-logic-card { border: 1px solid var(--line); border-radius: 8px; padding: 14px; background: #fff; }
    .step-logic-card h2 { margin-bottom: 10px; }
    .optional-step[hidden] { display: none; }
    .calc-column-list { display: grid; gap: 10px; margin-top: 12px; }
    .calc-column-row {
      display: grid;
      grid-template-columns: 92px minmax(120px, 180px) minmax(150px, 220px) minmax(120px, 150px) minmax(320px, 1fr) auto;
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
    .management-grid { display: grid; grid-template-columns: 360px minmax(0, 1fr); gap: 18px; align-items: start; margin-top: 18px; }
    .table-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .table-actions form { margin: 0; }
    @media (max-width: 900px) {
      .header-inner { align-items: flex-start; flex-direction: column; padding: 14px 0; gap: 12px; }
      .grid, .rule-layout, .split-layout, .form-row { grid-template-columns: 1fr; }
      .management-grid { grid-template-columns: 1fr; }
      .progressive-workspace { grid-template-columns: 200px 240px minmax(560px, 1fr) minmax(380px, 460px); overflow-x: auto; }
      .stage-column { min-height: 520px; max-height: none; }
      .flow-middle { grid-template-columns: 1fr; }
      .flow-section-head, .flow-selector-row { grid-template-columns: 1fr; flex-direction: column; }
      .step-diagram-wrap { grid-template-columns: repeat(2, minmax(190px, 1fr)); }
      .diagram-step-card.has-next::after, .diagram-step-card.wrap-start::before { display: none; }
      .flow-workbench { grid-template-columns: 1fr; }
      .calc-column-row, .modal .calc-column-row, .step-mode-grid { grid-template-columns: 1fr; }
      .checkbox-grid { grid-template-columns: 1fr; }
      main { width: min(100vw - 20px, 1680px); }
      .table-wrap { max-height: 62vh; }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <div class="brand">工厂加工费计算系统<small>本系统借助Codex编写而成，如需帮助，请联系周天旭 13631554910</small></div>
      <nav>
        <a class="{{ 'active' if page == 'periods' else '' }}" href="{{ url_for('periods_page', period=selected_period or cfg.yyyymm) }}">期间管理</a>
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

    {% if page == 'periods' %}
      <h1>期间管理</h1>
      <p>使用说明：财务期间是月度计算工作的容器。先选择当前期间，再进入后续页面导入数据、执行计算和导出结果。</p>
      <section class="panel" style="margin-top:0">
        <h2>当前期间</h2>
        <p class="muted">当前系统所有业务页面默认跟随这个期间。封存或归档后，只允许查看、下载和追溯。</p>
        <form method="get" action="{{ url_for('periods_page') }}" class="form-row">
          <label for="period_picker">期间</label>
          <select id="period_picker" name="period" onchange="this.form.submit()">
            {% for p in periods %}
              <option value="{{ p.period_id }}" {% if p.period_id == selected_period %}selected{% endif %}>{{ p.period_id }}｜{{ p.status_label }}</option>
            {% endfor %}
          </select>
        </form>
        <div class="grid">
          <div class="metric"><span class="muted">当前状态</span><strong>{{ period_context.current.status_label }}</strong></div>
          <div class="metric"><span class="muted">本期事实表</span><strong style="font-size:18px">{{ period_context.current_sap.import_batch_id or '未导入' }}</strong></div>
          <div class="metric"><span class="muted">最近计算任务</span><strong style="font-size:18px">{{ period_context.current_merge.merge_run_id or '无' }}</strong></div>
          <div class="metric"><span class="muted">最近导出版本</span><strong style="font-size:18px">{{ period_context.current_export.export_id or '无' }}</strong></div>
        </div>
        <div class="button-row">
          <form class="inline-form" method="post" action="{{ url_for('update_period_status', period_id=selected_period, action='complete', period=selected_period) }}"><button class="button secondary" type="submit">标记完成</button></form>
          <form class="inline-form" method="post" action="{{ url_for('update_period_status', period_id=selected_period, action='close', period=selected_period) }}"><button class="button secondary" type="submit">封存期间</button></form>
          <form class="inline-form" method="post" action="{{ url_for('update_period_status', period_id=selected_period, action='reopen', period=selected_period) }}"><button class="button secondary" type="submit">重新打开</button></form>
          <form class="inline-form" method="post" action="{{ url_for('update_period_status', period_id=selected_period, action='archive', period=selected_period) }}"><button class="button secondary" type="submit">归档期间</button></form>
        </div>
      </section>
      <section class="panel">
        <h2>新增期间</h2>
        <p class="muted">创建新的财务期间，建议使用 YYYYMM 格式，例如 202506。</p>
        <form id="new-period" method="post" action="{{ url_for('create_period') }}" class="form-row">
          <label for="new_period_id">新增期间</label>
          <div class="button-row" style="margin-top:0">
            <input id="new_period_id" name="period_id" type="text" placeholder="例如 202506" style="max-width:260px">
            <button type="submit">创建期间</button>
          </div>
        </form>
      </section>
      <section class="panel">
        <h2>期间列表</h2>
        <p class="muted">展示所有财务期间及其关键数据。点击“切换”后，后续页面默认进入该期间。</p>
        <div class="table-wrap">
          <table>
            <thead><tr><th>期间</th><th>状态</th><th>事实表批次</th><th>最近计算任务</th><th>最近导出版本</th><th>创建时间</th><th>封存时间</th><th>说明</th><th>操作</th></tr></thead>
            <tbody>
              {% for row in period_context.rows %}
                <tr>
                  <td>{{ row.period_id }}</td>
                  <td>{{ row.status_label }}</td>
                  <td>{{ row.fact_batch_label }}</td>
                  <td>{{ row.latest_task_label }}</td>
                  <td>{{ row.latest_export_label }}</td>
                  <td>{{ row.created_at }}</td>
                  <td>{{ row.closed_at or row.archived_at or '-' }}</td>
                  <td>{{ row.note or '-' }}</td>
                  <td>
                    {% if row.is_current %}
                      <span class="muted">当前</span>
                    {% else %}
                      <a class="button secondary" href="{{ url_for('periods_page', period=row.period_id) }}">切换</a>
                    {% endif %}
                  </td>
                </tr>
              {% else %}
                <tr><td colspan="9" class="muted">暂无期间</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        <p class="muted">规则：事实表、计算结果、加工费导出强绑定期间；配置表版本和计算流程可跨期间复用。</p>
      </section>
    {% endif %}

    {% if page == 'dashboard' %}
      <h1>执行计算</h1>
      <p>使用说明：选择事实表批次、计算流程和配置表版本，确认后发起计算。完成后在下方任务列表中查看结果。</p>
      <section class="panel">
        <h2>发起计算</h2>
        <p class="muted">把一个事实表批次，按选定流程和配置版本执行完整计算。</p>
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
          <p class="muted">本次运行将生成完整计算结果，并保留事实表批次、流程、配置表版本的追溯信息。</p>
          <div class="actions">
            <button type="submit">开始计算</button>
            <a class="button secondary" href="{{ url_for('index', period=selected_period, flow=selected_flow_key) }}">清空</a>
          </div>
        </form>
      </section>
      <section class="panel">
        <h2>已完成计算任务列表</h2>
        <p class="muted">这里统一展示最近完成的计算任务。点击“查看结果”后，跳转到计算结果页面查看明细、异常和回溯信息。</p>
        <div class="table-wrap">
          <table>
            <thead><tr><th>计算任务ID</th><th>事实表批次</th><th>计算流程</th><th>结果行数</th><th>异常行数</th><th>状态</th><th>完成时间</th><th>配置版本摘要</th><th>操作</th></tr></thead>
            <tbody>
              {% for row in summary.merge_runs %}
                <tr>
                  <td>{{ row.merge_run_id }}</td>
                  <td>{{ row.import_batch_id }}</td>
                  <td>{{ row.flow_name }}</td>
                  <td>{{ row.merged_rows }}</td>
                  <td>{{ row.exception_rows }}</td>
                  <td>{{ row.status_label }}</td>
                  <td>{{ row.finished_at }}</td>
                  <td>{{ row.config_versions_summary }}</td>
                  <td><a class="button secondary" href="{{ url_for('results', period=selected_period, merge_run_id=row.merge_run_id) }}">查看结果</a></td>
                </tr>
              {% else %}
                <tr><td colspan="9" class="muted">暂无已完成计算任务</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </section>
    {% elif page == 'sap' %}
      <h1>事实表管理</h1>
      <p>使用说明：上传上游系统数据，如SAP、决算系统数据，格式不限。</p>
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
        <h2>已导入数据</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>SAP导入批次</th><th>年月</th><th>工厂范围</th><th>文件名</th><th>导入行数</th><th>导入时间</th><th>状态</th><th>操作</th></tr></thead>
            <tbody>
              {% for batch in batches %}
                <tr>
                  <td>{{ batch.import_batch_id }}</td><td>{{ batch.yyyymm }}</td><td>{{ batch.factory_scope }}</td><td>{{ batch.source_file_name }}</td><td>{{ batch.import_rows }}</td><td>{{ batch.imported_at }}</td><td>{{ batch.status_label }}</td>
                  <td>
                    <div class="button-row" style="margin-top:0">
                      <a class="button secondary" href="{{ url_for('sap_data', period=selected_period, import_batch_id=batch.import_batch_id) }}">预览</a>
                      <a class="button secondary" href="{{ url_for('download_sap_batch', import_batch_id=batch.import_batch_id, period=selected_period) }}">下载</a>
                      <form method="post" action="{{ url_for('delete_sap_batch', import_batch_id=batch.import_batch_id, period=selected_period) }}"><button class="button secondary" type="submit">删除</button></form>
                    </div>
                  </td>
                </tr>
              {% else %}
                <tr><td colspan="8" class="muted">暂无 SAP 导入批次</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </section>
      {% if selected_batch %}
        <div class="modal-backdrop">
          <section class="modal" role="dialog" aria-modal="true" aria-labelledby="sap-preview-title">
            <div class="modal-head">
              <div>
                <h2 id="sap-preview-title">SAP 数据预览</h2>
                <p>仅展示前 100 行数据，支持上下滑动和左右滚动。</p>
              </div>
              <a class="button secondary" href="{{ url_for('sap_data', period=selected_period) }}">关闭</a>
            </div>
            <div class="modal-summary">
              <span class="summary-chip">批次 {{ selected_batch }}</span>
              <span class="summary-chip">年月 {{ selected_batch_meta.yyyymm or '-' }}</span>
              <span class="summary-chip">文件 {{ selected_batch_meta.source_file_name or '-' }}</span>
              <span class="summary-chip">导入行数 {{ preview.row_count }}</span>
              <span class="summary-chip">状态 {{ selected_batch_meta.status_label or '-' }}</span>
            </div>
            <div class="modal-body">
              <div class="table-wrap modal-table">
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
            </div>
            <div class="modal-foot">
              <a class="button secondary" href="{{ url_for('download_sap_batch', import_batch_id=selected_batch, period=selected_period) }}">下载完整数据</a>
              <a class="button" href="{{ url_for('sap_data', period=selected_period) }}">关闭</a>
            </div>
          </section>
        </div>
      {% endif %}
    {% elif page == 'rules' %}
      <h1>{% if rules_tab == 'tables' %}配置表管理{% else %}计算流程设置{% endif %}</h1>
      <p>{% if rules_tab == 'tables' %}使用说明：将主数据、系数表等用于计算、匹配的表格导入此页面。{% else %}使用说明：选择一个计算流程，在步骤模块管理流程步骤，步骤图解仅用于理解流程。{% endif %}</p>

      {% if rules_tab == 'tables' %}
        <div class="management-grid">
          <section class="panel" style="margin-top:0">
            <div class="button-row" style="margin-top:0; justify-content:space-between">
              <h2 style="margin:0">配置表类型</h2>
              <a class="button" href="{{ url_for('rules', tab='tables', table=selected, version=selected_version, new_type='1', period=selected_period) }}">新增类型</a>
            </div>
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
          </section>
          <section class="panel" style="margin-top:0">
            <div class="button-row" style="margin-top:0; justify-content:space-between">
              <div>
                <h2 style="margin:0">配置表版本</h2>
                <p>{% if rule_tables %}当前类型：{{ rule_tables[selected].name }}{% else %}请先创建配置表类型{% endif %}</p>
              </div>
              {% if rule_tables %}
                <a class="button" href="{{ url_for('rules', tab='tables', table=selected, version=selected_version, new_version='1', period=selected_period) }}">导入配置表</a>
              {% endif %}
            </div>
            <div class="table-wrap" style="margin-top:16px">
              <table>
                <thead><tr><th>配置表类型</th><th>版本名称</th><th>版本编码</th><th>创建人</th><th>创建时间</th><th>状态</th><th>操作</th></tr></thead>
                <tbody>
                  {% for version in config_versions %}
                    <tr>
                      <td>{{ rule_tables[selected].name }}</td>
                      <td>{{ version.name }}</td>
                      <td>{{ version.version_id }}</td>
                      <td>{{ version.created_by or 'admin' }}</td>
                      <td>{{ version.created_at or '' }}</td>
                      <td>{{ version.status_label }}</td>
                      <td>
                        <div class="table-actions">
                          <a class="button secondary" href="{{ url_for('rules', tab='tables', table=selected, version=version.version_id, preview='1', period=selected_period) }}">预览</a>
                          <a class="button secondary" href="{{ url_for('download_rule', table_key=selected, version=version.version_id, period=selected_period) }}">下载</a>
                          <form method="post" action="{{ url_for('delete_rule_table_data', table_key=selected) }}"><input type="hidden" name="period" value="{{ selected_period }}"><input type="hidden" name="version" value="{{ version.version_id }}"><button class="button secondary" type="submit">删除</button></form>
                        </div>
                      </td>
                    </tr>
                  {% else %}
                    <tr><td colspan="7" class="muted">暂无配置表版本，请点击“导入配置表”。</td></tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </section>
        </div>
        {% if show_new_type %}
          <div class="modal-backdrop">
            <section class="modal" role="dialog" aria-modal="true" aria-labelledby="new-type-title" style="width:min(720px, 92vw); height:auto;">
              <div class="modal-head">
                <div>
                  <h2 id="new-type-title">新增配置表类型</h2>
                  <p>先定义配置表类型，再导入对应版本。</p>
                </div>
                <a class="button secondary" href="{{ url_for('rules', tab='tables', table=selected, version=selected_version, period=selected_period) }}">关闭</a>
              </div>
              <form method="post" action="{{ url_for('create_rule_table') }}">
                <div class="modal-body" style="overflow:auto; padding-top:18px">
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
                </div>
                <div class="modal-foot">
                  <a class="button secondary" href="{{ url_for('rules', tab='tables', table=selected, version=selected_version, period=selected_period) }}">取消</a>
                  <button type="submit">保存类型</button>
                </div>
              </form>
            </section>
          </div>
        {% endif %}
        {% if show_new_version and rule_tables %}
          <div class="modal-backdrop">
            <section class="modal" role="dialog" aria-modal="true" aria-labelledby="new-version-title" style="width:min(860px, 92vw); height:auto;">
              <div class="modal-head">
                <div>
                  <h2 id="new-version-title">导入配置表</h2>
                  <p>当前类型：{{ rule_tables[selected].name }}</p>
                </div>
                <a class="button secondary" href="{{ url_for('rules', tab='tables', table=selected, version=selected_version, period=selected_period) }}">关闭</a>
              </div>
              <form method="post" enctype="multipart/form-data" action="{{ url_for('upload_rule', table_key=selected) }}">
                <input type="hidden" name="period" value="{{ selected_period }}">
                <div class="modal-body" style="overflow:auto; padding-top:18px">
                  <div class="form-row">
                    <label>配置表类型</label>
                    <input type="text" value="{{ rule_tables[selected].name }}" disabled>
                  </div>
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
                    <label for="file">上传文件</label>
                    <input id="file" name="file" type="file" accept=".csv,.xlsx,.xls">
                  </div>
                </div>
                <div class="modal-foot">
                  <a class="button secondary" href="{{ url_for('rules', tab='tables', table=selected, version=selected_version, period=selected_period) }}">取消</a>
                  <button type="submit">导入配置表</button>
                </div>
              </form>
            </section>
          </div>
        {% endif %}
        {% if show_table_preview and selected_version %}
          <div class="modal-backdrop">
            <section class="modal" role="dialog" aria-modal="true" aria-labelledby="config-preview-title">
              <div class="modal-head">
                <div>
                  <h2 id="config-preview-title">配置表数据预览</h2>
                  <p>仅展示前 100 行数据，支持上下滑动和左右滚动。</p>
                </div>
                <a class="button secondary" href="{{ url_for('rules', tab='tables', table=selected, version=selected_version, period=selected_period) }}">关闭</a>
              </div>
              <div class="modal-summary">
                <span class="summary-chip">类型 {{ rule_tables[selected].name if rule_tables else '-' }}</span>
                <span class="summary-chip">版本 {{ selected_version }}</span>
                <span class="summary-chip">总行数 {{ preview.row_count }}</span>
                {% for version in config_versions if version.version_id == selected_version %}
                  <span class="summary-chip">创建人 {{ version.created_by or 'admin' }}</span>
                  <span class="summary-chip">状态 {{ version.status_label }}</span>
                {% endfor %}
              </div>
              <div class="modal-body">
                <div class="table-wrap modal-table">
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
              </div>
              <div class="modal-foot">
                <a class="button secondary" href="{{ url_for('download_rule', table_key=selected, version=selected_version, period=selected_period) }}">下载完整数据</a>
                <a class="button" href="{{ url_for('rules', tab='tables', table=selected, version=selected_version, period=selected_period) }}">关闭</a>
              </div>
            </section>
          </div>
        {% endif %}
      {% else %}
        {% if merge_flows %}
        {% set active_step_index = selected_step_index %}
        {% set active_step = namespace(name='未选择') %}
        {% for step in flow_config.steps if step.index == active_step_index %}
          {% set active_step.name = step.name %}
        {% endfor %}
        <form id="flow-config-form" method="post" action="{{ url_for('update_merge_flow', flow_key=flow_config.flow_key) }}">
          <input type="hidden" name="period" value="{{ selected_period }}">
          <input type="hidden" name="selected_step" value="{{ active_step_index }}">
          <section class="flow-section">
            <div class="flow-section-head">
              <div class="flow-section-title">
                <h2>流程选择</h2>
                <span class="muted">先选择流程，再维护下方步骤。</span>
              </div>
              <div class="flow-actions">
                <a class="button" href="{{ url_for('rules', tab='flows', new_flow='1', period=selected_period) }}">新增流程</a>
                <button class="button secondary" type="submit" formaction="{{ url_for('delete_merge_flow', flow_key=flow_config.flow_key, period=selected_period) }}" formmethod="post">删除流程</button>
                <button type="submit">保存流程</button>
              </div>
            </div>
            <div class="flow-selector-row">
              <strong>当前流程</strong>
              <select onchange="window.location='{{ url_for('rules', tab='flows', period=selected_period) }}&flow=' + this.value + '&selected_step=1'">
                {% for flow in merge_flows %}
                  <option value="{{ flow.key }}" {% if flow.key == selected_flow_key %}selected{% endif %}>{{ flow.name }}</option>
                {% endfor %}
              </select>
              <span class="muted">{{ flow_config.steps|length }} 个步骤｜{{ flow_config.calculated_columns|selectattr('active')|list|length }} 个计算字段</span>
            </div>
          </section>

          {% for step in flow_config.steps if step.index != edit_step_index %}
            <input type="hidden" name="step_index" value="{{ step.index }}">
            <input type="hidden" name="step_{{ step.index }}_name" value="{{ step.name }}">
            <input type="hidden" name="step_{{ step.index }}_category" value="{{ step.step_category }}">
            <input type="hidden" name="step_{{ step.index }}_action" value="{{ step.step_action }}">
            {% if step.step_action == 'merge' %}
              <input type="hidden" name="step_{{ step.index }}_rule_table" value="{{ step.right_table }}">
              {% for field in step.selected_fields %}<input type="hidden" name="step_{{ step.index }}_match_fields" value="{{ field }}">{% endfor %}
              {% for field in step.selected_output_fields %}<input type="hidden" name="step_{{ step.index }}_output_fields" value="{{ field }}">{% endfor %}
            {% else %}
              {% for col in step.calculated_columns %}
                <input type="hidden" name="step_{{ step.index }}_calc_index" value="{{ col.index }}">
                <input type="hidden" name="step_{{ step.index }}_calc_{{ col.index }}_name" value="{{ col.name }}">
                <input type="hidden" name="step_{{ step.index }}_calc_{{ col.index }}_field" value="{{ col.field }}">
                <input type="hidden" name="step_{{ step.index }}_calc_{{ col.index }}_method" value="{{ col.method }}">
                <input type="hidden" name="step_{{ step.index }}_calc_{{ col.index }}_formula" value="{{ col.formula }}">
                {% if col.active %}<input type="hidden" name="step_{{ step.index }}_calc_{{ col.index }}_active" value="Y">{% endif %}
              {% endfor %}
            {% endif %}
          {% endfor %}

          <section class="flow-section">
            <div class="flow-section-head">
              <div class="flow-section-title">
                <h2>步骤管理</h2>
                <span class="muted">当前选中：{% if active_step_index %}步骤{{ active_step_index }} {{ active_step.name }}{% else %}未选择{% endif %}</span>
              </div>
              <div class="flow-actions">
                <button class="button" type="submit" formaction="{{ url_for('add_merge_flow_step', flow_key=flow_config.flow_key, period=selected_period) }}" formmethod="post">新增步骤</button>
                <button class="button secondary" type="submit" formaction="{{ url_for('move_merge_flow_step', flow_key=flow_config.flow_key, step_index=active_step_index, direction='up', period=selected_period) }}" formmethod="post" {% if not active_step_index %}disabled{% endif %}>上移</button>
                <button class="button secondary" type="submit" formaction="{{ url_for('move_merge_flow_step', flow_key=flow_config.flow_key, step_index=active_step_index, direction='down', period=selected_period) }}" formmethod="post" {% if not active_step_index %}disabled{% endif %}>下移</button>
                <button class="button secondary" type="submit" formaction="{{ url_for('delete_merge_flow_step', flow_key=flow_config.flow_key, step_index=active_step_index, period=selected_period) }}" formmethod="post" {% if not active_step_index %}disabled{% endif %}>删除步骤</button>
              </div>
            </div>
            <div class="table-wrap" style="margin-top:14px; max-height:none">
              <table>
                <thead><tr><th class="step-radio-cell">选择</th><th>顺序</th><th>步骤名称</th><th>步骤类型</th><th>左侧数据源</th><th>配置对象</th><th>关键字段</th><th>输出/目标</th><th>操作</th></tr></thead>
                <tbody>
                  {% for step in flow_config.steps %}
                    <tr class="flow-step-row {% if step.index == active_step_index %}active{% endif %}" data-step-url="{{ url_for('rules', tab='flows', flow=flow_config.flow_key, selected_step=step.index, period=selected_period) }}" onclick="window.location=this.dataset.stepUrl">
                      <td class="step-radio-cell">
                        <input class="step-radio" type="radio" name="visible_selected_step" value="{{ step.index }}" aria-label="选择步骤{{ step.index }}" {% if step.index == active_step_index %}checked{% endif %} onclick="event.stopPropagation(); window.location=this.closest('tr').dataset.stepUrl">
                      </td>
                      <td>{{ step.index }}</td>
                      <td>{{ step.name }}</td>
                      <td>{{ step.step_category_label }} / {{ step.step_action_label }}</td>
                      <td>{{ step.left_source_name }}</td>
                      <td>{% if step.step_action == 'merge' %}{{ step.right_table_name }}{% else %}字段计算{% endif %}</td>
                      <td>{% if step.step_action == 'merge' %}{{ step.selected_labels|join('、') or '未配置' }}{% else %}{{ step.calculated_columns|length }} 个公式{% endif %}</td>
                      <td>{% if step.step_action == 'merge' %}{{ step.selected_output_labels[:4]|join('、') }}{% if step.selected_output_labels|length > 4 %}等{% endif %}{% else %}{% for col in step.calculated_columns[:4] %}{{ col.name }}{% if not loop.last %}、{% endif %}{% else %}未配置{% endfor %}{% endif %}</td>
                      <td><a class="button secondary" onclick="event.stopPropagation()" href="{{ url_for('rules', tab='flows', flow=flow_config.flow_key, selected_step=step.index, edit_step=step.index, period=selected_period) }}">编辑</a></td>
                    </tr>
                  {% else %}
                    <tr><td colspan="9" class="muted">暂无步骤，请点击新增步骤。</td></tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </section>

          <section class="flow-section">
            <div class="flow-section-head">
              <div class="flow-section-title">
                <h2>步骤图解</h2>
              </div>
            </div>
            <div class="step-diagram-wrap">
              {% for step in flow_config.steps %}
                <div class="diagram-step-card {% if step.index == active_step_index %}active{% endif %} {% if not loop.last and loop.index % 4 != 0 %}has-next{% endif %} {% if loop.index > 1 and loop.index % 4 == 1 %}wrap-start{% endif %}">
                  <strong>步骤{{ step.index }}：{{ step.name }}</strong>
                  <span>类型：{{ step.step_category_label }} / {{ step.step_action_label }}</span>
                  <span>来源：{{ step.left_source_name }}</span>
                  {% if step.step_action == 'merge' %}
                    <span>配置表：{{ step.right_table_name }}</span>
                    <span>匹配：{{ step.selected_labels|join('、') or '未配置' }}</span>
                    <span>输出：{{ step.selected_output_labels[:4]|join('、') }}{% if step.selected_output_labels|length > 4 %}等{% endif %}</span>
                  {% else %}
                    <span>计算：{{ step.calculated_columns|length }} 个公式</span>
                    <span>字段：{% for col in step.calculated_columns[:3] %}{{ col.name }}{% if not loop.last %}、{% endif %}{% else %}未配置{% endfor %}</span>
                  {% endif %}
                </div>
              {% else %}
                <p class="muted">暂无步骤，请先新增步骤。</p>
              {% endfor %}
            </div>
          </section>

          {% if edit_step_index %}
            {% for step in flow_config.steps if step.index == edit_step_index %}
              <div class="modal-backdrop">
                <section class="modal" role="dialog" aria-modal="true" aria-labelledby="flow-step-modal-title">
                  <div class="modal-head">
                    <div>
                      <h2 id="flow-step-modal-title">编辑步骤详情</h2>
                      <p>按“新增字段”和“修改字段”两个维度维护当前步骤。</p>
                    </div>
                    <a class="button secondary" href="{{ url_for('rules', tab='flows', flow=flow_config.flow_key, selected_step=step.index, period=selected_period) }}">关闭</a>
                  </div>
                  <div class="modal-body">
                    <input type="hidden" name="step_index" value="{{ step.index }}">
                    <input type="hidden" name="return_edit_step" value="{{ step.index }}">
                    <section class="step-logic-card">
                      <div class="step-mode-grid">
                        <div class="form-row" style="margin-top:0">
                          <label>步骤类型</label>
                          <select name="step_{{ step.index }}_category" onchange="this.form.submit()">
                            <option value="add_field" {% if step.step_category == 'add_field' %}selected{% endif %}>新增字段</option>
                            <option value="modify_field" {% if step.step_category == 'modify_field' %}selected{% endif %}>修改字段</option>
                          </select>
                        </div>
                        <div class="form-row" style="margin-top:0">
                          <label>处理方式</label>
                          {% if step.step_category == 'modify_field' %}
                            <input type="hidden" name="step_{{ step.index }}_action" value="calculate">
                            <input type="text" value="计算" disabled>
                          {% else %}
                            <select name="step_{{ step.index }}_action" onchange="this.form.submit()">
                              <option value="merge" {% if step.step_action == 'merge' %}selected{% endif %}>合并：表与表匹配</option>
                              <option value="calculate" {% if step.step_action == 'calculate' %}selected{% endif %}>计算：字段公式</option>
                            </select>
                          {% endif %}
                        </div>
                      </div>
                      <div class="form-row">
                        <label>步骤名称</label>
                        <input name="step_{{ step.index }}_name" type="text" value="{{ step.name }}">
                      </div>
                    </section>

                    {% if step.step_action == 'merge' %}
                      <section class="step-logic-card" style="margin-top:14px">
                        <h2>表与表的匹配</h2>
                        <div class="version-meta" style="margin-top:0">
                          <div><strong>左侧数据源</strong><br>{{ step.left_source_name }}<br><span class="muted">{{ step.left_source_detail }}</span></div>
                          <div><strong>右侧配置表</strong><br>{{ step.right_table_name }}<br><span class="muted">运行时选择具体配置表版本</span></div>
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
                        <p class="muted" style="margin-top:6px">{{ step.left_source_name }} 与当前配置表都存在的字段才会显示在这里。</p>
                        <div class="checkbox-grid">
                          {% for item in step.match_options %}
                            <label class="check-item">
                              <input type="checkbox" name="step_{{ step.index }}_match_fields" value="{{ item.field }}" {% if item.field in step.selected_fields %}checked{% endif %}>
                              <span>{{ item.label }}</span>
                            </label>
                          {% else %}
                            <span class="muted">当前配置表暂无可匹配字段。</span>
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
                      </section>
                    {% else %}
                      <section class="step-logic-card" style="margin-top:14px">
                        <div class="button-row" style="margin-top:0; justify-content:space-between">
                          <div>
                            <h2 style="margin:0">{% if step.step_category == 'modify_field' %}修改字段计算{% else %}新增字段计算{% endif %}</h2>
                            <p class="muted" style="margin:6px 0 0">{% if step.step_category == 'modify_field' %}用公式改写已有字段，后续会补齐格式和重命名能力。{% else %}用公式生成新的字段，后一行公式可以引用前面已生成字段。{% endif %}</p>
                          </div>
                          <button class="button secondary" id="add-step-calc-column" type="button">新增计算字段</button>
                        </div>
                        <div id="step-calc-column-list" class="calc-column-list">
                          {% for col in step.calculated_columns %}
                            <div class="calc-column-row">
                              <input type="hidden" name="step_{{ step.index }}_calc_index" value="{{ col.index }}">
                              <label class="check-item" style="min-height:38px"><input type="checkbox" name="step_{{ step.index }}_calc_{{ col.index }}_active" value="Y" {% if col.active %}checked{% endif %}><span>启用</span></label>
                              <input name="step_{{ step.index }}_calc_{{ col.index }}_name" type="text" value="{{ col.name }}" placeholder="字段名称">
                              <input name="step_{{ step.index }}_calc_{{ col.index }}_field" type="text" value="{{ col.field }}" placeholder="字段编码">
                              <select name="step_{{ step.index }}_calc_{{ col.index }}_method" aria-label="计算方式">
                                <option value="formula" {% if col.method == 'formula' %}selected{% endif %}>公式</option>
                                <option value="sql" {% if col.method == 'sql' %}selected{% endif %}>SQL表达式</option>
                              </select>
                              <input name="step_{{ step.index }}_calc_{{ col.index }}_formula" type="text" value="{{ col.formula }}" placeholder="计算公式">
                              <button class="button secondary remove-calc-column" type="button">删除</button>
                            </div>
                          {% else %}
                            <p class="muted">暂无计算字段，请点击右上角新增。</p>
                          {% endfor %}
                        </div>
                        <template id="step-calc-column-template">
                          <div class="calc-column-row">
                            <input type="hidden" name="step_{{ step.index }}_calc_index" value="__INDEX__">
                            <label class="check-item" style="min-height:38px"><input type="checkbox" name="step_{{ step.index }}_calc___INDEX___active" value="Y" checked><span>启用</span></label>
                            <input name="step_{{ step.index }}_calc___INDEX___name" type="text" placeholder="字段名称">
                            <input name="step_{{ step.index }}_calc___INDEX___field" type="text" placeholder="字段编码">
                            <select name="step_{{ step.index }}_calc___INDEX___method" aria-label="计算方式">
                              <option value="formula" selected>公式</option>
                              <option value="sql">SQL表达式</option>
                            </select>
                            <input name="step_{{ step.index }}_calc___INDEX___formula" type="text" placeholder="计算公式，例如 num(delivery_qty) * 1.2">
                            <button class="button secondary remove-calc-column" type="button">删除</button>
                          </div>
                        </template>
                      </section>
                    {% endif %}
                  </div>
                  <div class="modal-foot">
                    <button class="button secondary" type="submit" formaction="{{ url_for('delete_merge_flow_step', flow_key=flow_config.flow_key, step_index=step.index, period=selected_period) }}" formmethod="post">删除步骤</button>
                    <button type="submit">保存步骤</button>
                  </div>
                </section>
              </div>
              <script>
                (() => {
                  const list = document.getElementById('step-calc-column-list');
                  const template = document.getElementById('step-calc-column-template');
                  const addButton = document.getElementById('add-step-calc-column');
                  let nextIndex = {{ (step.calculated_columns|length) + 1 }};
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
            {% endfor %}
          {% endif %}
        </form>
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
      <h1>使用指引</h1>
      <p>本系统用于把上游事实数据、配置表版本和计算流程组合起来，生成可追溯的加工费计算结果，并输出面向业务用户的结果表。</p>
      <section class="panel">
        <h2>推荐操作流程</h2>
        <div class="grid">
          <div class="metric"><span class="muted">准备</span><strong>期间管理</strong><p>创建或切换当前财务期间。后续页面默认跟随当前期间。</p></div>
          <div class="metric"><span class="muted">第一步</span><strong>事实表管理</strong><p>上传上游系统数据，如 SAP、决算系统数据。系统生成导入批次，并保留原始明细。</p></div>
          <div class="metric"><span class="muted">第二步</span><strong>配置表管理</strong><p>创建配置表类型，再导入主数据、系数表、单价表等配置版本。</p></div>
          <div class="metric"><span class="muted">第三步</span><strong>计算流程设置</strong><p>维护流程步骤。一个步骤只做一件事：合并新增字段、计算新增字段，或计算修改字段。</p></div>
          <div class="metric"><span class="muted">第四步</span><strong>执行计算</strong><p>选择事实表批次、计算流程和各步骤配置表版本，点击开始计算。</p></div>
          <div class="metric"><span class="muted">第五步</span><strong>计算结果</strong><p>查看完整回溯结果、异常信息、配置版本来源和每一步命中情况。</p></div>
          <div class="metric"><span class="muted">第六步</span><strong>加工费导出</strong><p>勾选业务用户需要的字段，生成可留存、可下载的加工费导出版本。</p></div>
        </div>
      </section>
      <section class="panel">
        <h2>页面说明</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>页面</th><th>主要用途</th><th>怎么使用</th><th>注意事项</th></tr></thead>
            <tbody>
              <tr><td>期间管理</td><td>管理财务期间，作为月度计算工作的顶层容器。</td><td>创建、切换、完成、封存、重新打开期间。</td><td>封存或归档期间只允许查看、下载和追溯。</td></tr>
              <tr><td>1 事实表管理</td><td>导入事实数据，管理已导入批次。</td><td>选择文件后导入；在列表中可预览、下载、删除。</td><td>导入时不强制字段校验，系统尽量保留原始表头。</td></tr>
              <tr><td>2 配置表管理</td><td>管理参与计算和匹配的配置表。</td><td>先新增配置表类型，再在该类型下新增配置表版本。</td><td>明细数据通过弹窗查看；删除版本只影响当前版本。</td></tr>
              <tr><td>3 计算流程设置</td><td>定义计算流程和步骤顺序。</td><td>选择流程，在步骤管理中新增、编辑、上移、下移、删除步骤。</td><td>流程只绑定配置表类型；正式运行时再选择具体版本。</td></tr>
              <tr><td>4 执行计算</td><td>发起正式计算任务。</td><td>选择事实表批次、流程和各步骤版本，点击开始计算。</td><td>页面下方的已完成任务可直接跳转到计算结果。</td></tr>
              <tr><td>5 计算结果</td><td>查看完整计算明细和异常。</td><td>选择结果批次后查看结果；可删除当前结果。</td><td>这里是回溯表，字段较多，不建议直接作为业务交付表。</td></tr>
              <tr><td>6 加工费导出</td><td>生成面向业务用户的结果表。</td><td>勾选需要展示的字段，预览同步变化后生成导出版本。</td><td>历史导出会保留，可按批次回看和下载。</td></tr>
              <tr><td>日志</td><td>查看系统运行记录。</td><td>用于追溯导入、计算、合并、异常和下载文件。</td><td>适合排查某次运行是否成功。</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>核心概念</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>概念</th><th>含义</th><th>例子</th></tr></thead>
            <tbody>
              <tr><td>事实表</td><td>每次计算的业务流水数据，通常来自上游系统。</td><td>SAP 月度加工费流水、决算系统数据。</td></tr>
              <tr><td>配置表类型</td><td>一类可复用配置表的定义。</td><td>物料主数据、难度系数表、单台加工费表、损益交货量表。</td></tr>
              <tr><td>配置表版本</td><td>某个配置表类型下的一份具体数据。</td><td>物料主数据 v_20260518_175940。</td></tr>
              <tr><td>计算流程</td><td>由多个步骤组成的计算方案。</td><td>标准流程1：先匹配物料主数据，再计算字段，再匹配系数表。</td></tr>
              <tr><td>步骤</td><td>流程中的一个动作。一个步骤只做一件事。</td><td>新增字段 / 合并，新增字段 / 计算，修改字段 / 计算。</td></tr>
              <tr><td>计算结果</td><td>系统运行后生成的完整明细，包含业务字段、命中状态和异常原因。</td><td>用于复核和追溯。</td></tr>
              <tr><td>加工费导出</td><td>从计算结果中挑选字段后生成的业务交付表。</td><td>用于汇报、传阅和归档。</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>配置表与流程设置</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>问题</th><th>说明</th></tr></thead>
            <tbody>
              <tr><td>配置表字段从哪里来？</td><td>来自上传的配置表版本。系统会读取表头，并在页面中优先展示中文字段名。</td></tr>
              <tr><td>匹配字段怎么出现？</td><td>选择配置表类型后，系统只展示左侧数据源和当前配置表都存在的字段。</td></tr>
              <tr><td>输出字段怎么出现？</td><td>输出字段来自当前配置表中未作为匹配字段的字段，用于把配置表中的业务属性带入结果。</td></tr>
              <tr><td>第一步可以匹配什么？</td><td>第一步左侧数据源是事实表，用事实表字段去匹配配置表字段。</td></tr>
              <tr><td>后续步骤可以匹配什么？</td><td>后续步骤左侧数据源是上一步结果，因此可以使用前面步骤已经带出的字段继续匹配。</td></tr>
              <tr><td>为什么某张表匹配不到？</td><td>常见原因是字段值不一致、配置表版本选错、字段表头不同、字段为空、流程步骤顺序不对。</td></tr>
              <tr><td>配置表允许新增字段吗？</td><td>允许。系统会保留上传表格中的额外字段，匹配和输出时按当前版本字段识别。</td></tr>
              <tr><td>删除配置表类型或版本要注意什么？</td><td>删除后会影响后续运行时的可选项；已经生成的历史结果仍可用于追溯。</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>步骤类型怎么选</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>步骤类型</th><th>处理方式</th><th>适合场景</th><th>例子</th></tr></thead>
            <tbody>
              <tr><td>新增字段</td><td>合并：表与表匹配</td><td>把配置表中的字段带入当前结果。</td><td>根据物料编码匹配物料描述、品牌、机型。</td></tr>
              <tr><td>新增字段</td><td>计算：字段公式</td><td>根据已有字段生成新的业务指标。</td><td>加工费CNY = 损益交货量 * 单台加工费CNY。</td></tr>
              <tr><td>修改字段</td><td>计算：字段公式</td><td>对已有字段做名称、口径或数值调整。</td><td>把空值改成 0，或把本币金额换算成 CNY。</td></tr>
            </tbody>
          </table>
        </div>
        <p class="muted">建议：每个步骤只做一件事。需要先合并再计算时，请拆成两个步骤，这样更容易复核，也更接近 PowerQuery 的操作习惯。</p>
      </section>
      <section class="panel">
        <h2>执行计算与查看结果</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>动作</th><th>说明</th><th>结果</th></tr></thead>
            <tbody>
              <tr><td>发起计算</td><td>在“4 执行计算”中选择事实表批次、计算流程和各步骤配置表版本。</td><td>生成一条计算任务，并自动跳转到计算结果页面。</td></tr>
              <tr><td>查看已完成任务</td><td>执行计算页下方展示已完成计算任务列表。</td><td>点击“查看结果”进入对应结果批次。</td></tr>
              <tr><td>查看计算结果</td><td>在“5 计算结果”中查看完整结果明细和异常明细。</td><td>用于复核匹配是否命中、字段是否正确、异常原因是什么。</td></tr>
              <tr><td>删除当前结果</td><td>在计算结果页删除当前选中的结果批次。</td><td>删除该批次结果和异常记录，请谨慎使用。</td></tr>
              <tr><td>生成加工费导出</td><td>在“6 加工费导出”中勾选字段并生成导出版本。</td><td>生成更适合业务查看和下载的结果表。</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section class="panel">
        <h2>计算字段：公式与SQL表达式</h2>
        <p>在计算步骤中，可以新增或修改字段。建议优先使用“公式”，门槛更低；遇到复杂条件判断时，再使用“SQL表达式”。</p>
        <div class="table-wrap">
          <table>
            <thead><tr><th>方式</th><th>适合场景</th><th>填写规则</th><th>示例</th></tr></thead>
            <tbody>
              <tr>
                <td>公式</td>
                <td>常规四则运算、金额、数量、系数、简单条件判断。</td>
                <td>直接引用字段编码；中文字段名可写成 [字段名]。后一行公式可以引用前面已生成的新字段。</td>
                <td><code>num(delivery_qty) * num(unit_fee_cny)</code></td>
              </tr>
              <tr>
                <td>SQL表达式</td>
                <td>复杂条件判断，例如 CASE WHEN、IN、COALESCE。</td>
                <td>只填写一个表达式，不写 SELECT、UPDATE、DELETE，也不要加分号。</td>
                <td><code>CASE WHEN currency_code = 'CNY' THEN unit_fee_local ELSE unit_fee_local * exchange_rate END</code></td>
              </tr>
            </tbody>
          </table>
        </div>
        <h3>公式常用写法</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>写法</th><th>含义</th><th>示例</th></tr></thead>
            <tbody>
              <tr><td><code>num(字段)</code></td><td>把字段转成数字，空值或异常值按 0 处理。</td><td><code>num(delivery_qty)</code></td></tr>
              <tr><td><code>num(字段, 默认值)</code></td><td>把字段转成数字，并指定空值时的默认值。</td><td><code>num(pnl_delivery_coef, 1)</code></td></tr>
              <tr><td><code>text(字段)</code></td><td>把字段转成文本。</td><td><code>text(currency_code)</code></td></tr>
              <tr><td><code>if_eq(字段, "值", 真值, 假值)</code></td><td>字段等于某个值时返回真值，否则返回假值。</td><td><code>if_eq(currency_code, "CNY", num(unit_fee_local), num(unit_fee_local) * num(exchange_rate))</code></td></tr>
              <tr><td><code>if_in(字段, "值1,值2", 真值, 假值)</code></td><td>字段属于多个值之一时返回真值，否则返回假值。</td><td><code>if_in(include_pnl_delivery, "是,Y,YES,1,TRUE", num(delivery_qty), 0)</code></td></tr>
              <tr><td><code>round(数值, 位数)</code></td><td>保留指定小数位。</td><td><code>round(num(fee_amount_cny), 2)</code></td></tr>
              <tr><td><code>coalesce(a, b)</code></td><td>优先取第一个有值的数据。</td><td><code>coalesce(model, material_model)</code></td></tr>
            </tbody>
          </table>
        </div>
        <h3>常见计算示例</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>业务含义</th><th>推荐写法</th></tr></thead>
            <tbody>
              <tr><td>损益交货量</td><td><code>if_in(include_pnl_delivery, "是,Y,YES,1,TRUE", num(delivery_qty) * num(pnl_delivery_coef, 1), 0)</code></td></tr>
              <tr><td>单台加工费CNY</td><td><code>if_eq(currency_code, "CNY", num(unit_fee_local), num(unit_fee_local) * num(exchange_rate))</code></td></tr>
              <tr><td>加工费CNY</td><td><code>num(pnl_delivery_qty) * num(unit_fee_cny)</code></td></tr>
              <tr><td>标准工时</td><td><code>num(std_hour_coef) * num(delivery_qty)</code></td></tr>
              <tr><td>综合难度</td><td><code>num(difficulty_coef) * num(delivery_qty)</code></td></tr>
            </tbody>
          </table>
        </div>
        <h3>SQL表达式示例</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>业务含义</th><th>SQL表达式</th></tr></thead>
            <tbody>
              <tr><td>币种换算</td><td><code>CASE WHEN currency_code = 'CNY' THEN unit_fee_local ELSE unit_fee_local * exchange_rate END</code></td></tr>
              <tr><td>空值兜底</td><td><code>COALESCE(unit_fee_cny, 0) * COALESCE(delivery_qty, 0)</code></td></tr>
              <tr><td>中文字段名引用</td><td><code>CASE WHEN [是否计入损益交货量] IN ('是','Y') THEN [交货数量] ELSE 0 END</code></td></tr>
            </tbody>
          </table>
        </div>
        <p class="muted">提示：公式和SQL表达式都只用于当前行计算，不用于跨行汇总。字段编码比中文名称更稳定；如果使用中文字段名，请用方括号包起来，例如 <code>[交货数量]</code>。</p>
      </section>
      <section class="panel">
        <h2>常见问题</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>问题</th><th>回答</th></tr></thead>
            <tbody>
              <tr><td>为什么页面字段尽量显示中文？</td><td>系统展示层会把内部字段名转换成中文，并尽量保留导入表格中的原始中文表头。</td></tr>
              <tr><td>计算结果和加工费导出有什么区别？</td><td>计算结果用于复核和追溯，字段多；加工费导出用于交付，字段可勾选。</td></tr>
              <tr><td>导出预览会随字段选择变化吗？</td><td>会。勾选或取消字段后，导出预览会同步变化。</td></tr>
              <tr><td>删除 SAP 批次会影响什么？</td><td>会同步删除该批次的原始行、相关计算结果、异常和运行记录。</td></tr>
              <tr><td>默认流程可以删除吗？</td><td>可以。删除后可重新新增流程并配置步骤。</td></tr>
              <tr><td>本地版适合什么场景？</td><td>适合单机使用、现场演示、规则验证和小范围试运行。数据保存在本机 SQLite 和文件目录中。</td></tr>
              <tr><td>未来部署到服务器需要补什么？</td><td>需要登录权限、数据备份、访问控制、Nginx/HTTPS、安全组收口和更完整的操作审计。</td></tr>
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

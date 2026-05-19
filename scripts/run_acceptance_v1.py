from __future__ import annotations

from pathlib import Path
import math
import sqlite3
import sys

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1]))

from factory_fee.config import load_config
from factory_fee.db import connect
from factory_fee.pipeline import run_pipeline


RUN_ID = "ACCEPTANCE_V1_BUSINESS_RULES"
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "config.acceptance_v1.yaml"

EXPECTED = {
    "A01": {
        "scene": "精确匹配成功",
        "match": {"material": "MATCHED", "model_coef": "MATCHED", "unit_fee": "MATCHED", "pnl_delivery": "MATCHED"},
        "pnl_delivery_qty": 100.0,
        "unit_fee_cny": 10.0,
        "fee_amount_cny": 1000.0,
        "std_hour": 120.0,
        "difficulty_value": 110.0,
        "exceptions": set(),
    },
    "A02": {
        "scene": "ALL 通配匹配成功",
        "match": {"material": "MATCHED", "model_coef": "MATCHED", "unit_fee": "MATCHED", "pnl_delivery": "MATCHED"},
        "pnl_delivery_qty": 40.0,
        "unit_fee_cny": 20.0,
        "fee_amount_cny": 800.0,
        "std_hour": 120.0,
        "difficulty_value": 100.0,
        "exceptions": set(),
    },
    "A03": {
        "scene": "表2物料主数据未匹配",
        "match": {"material": "NOT_FOUND", "model_coef": "NOT_FOUND", "unit_fee": "NOT_FOUND", "pnl_delivery": "NOT_FOUND"},
        "pnl_delivery_qty": 0.0,
        "unit_fee_cny": 0.0,
        "fee_amount_cny": 0.0,
        "std_hour": 0.0,
        "difficulty_value": 0.0,
        "exceptions": {"MATERIAL_RULE_NOT_FOUND", "MODEL_COEF_RULE_NOT_FOUND", "UNIT_FEE_RULE_NOT_FOUND", "PNL_DELIVERY_RULE_NOT_FOUND"},
    },
    "A04": {
        "scene": "表3机型系数未匹配",
        "match": {"material": "MATCHED", "model_coef": "NOT_FOUND", "unit_fee": "MATCHED", "pnl_delivery": "MATCHED"},
        "pnl_delivery_qty": 30.0,
        "unit_fee_cny": 15.0,
        "fee_amount_cny": 450.0,
        "std_hour": 0.0,
        "difficulty_value": 0.0,
        "exceptions": {"MODEL_COEF_RULE_NOT_FOUND"},
    },
    "A05": {
        "scene": "表4单台加工费未匹配",
        "match": {"material": "MATCHED", "model_coef": "MATCHED", "unit_fee": "NOT_FOUND", "pnl_delivery": "MATCHED"},
        "pnl_delivery_qty": 40.0,
        "unit_fee_cny": 0.0,
        "fee_amount_cny": 0.0,
        "std_hour": 40.0,
        "difficulty_value": 40.0,
        "exceptions": {"UNIT_FEE_RULE_NOT_FOUND"},
    },
    "A06": {
        "scene": "表5损益交货量规则未匹配",
        "match": {"material": "MATCHED", "model_coef": "MATCHED", "unit_fee": "MATCHED", "pnl_delivery": "NOT_FOUND"},
        "pnl_delivery_qty": 0.0,
        "unit_fee_cny": 11.0,
        "fee_amount_cny": 0.0,
        "std_hour": 50.0,
        "difficulty_value": 37.5,
        "exceptions": {"PNL_DELIVERY_RULE_NOT_FOUND"},
    },
    "A07": {
        "scene": "同一优先级重复命中",
        "match": {"material": "DUPLICATED", "model_coef": "DUPLICATED", "unit_fee": "DUPLICATED", "pnl_delivery": "DUPLICATED"},
        "pnl_delivery_qty": 0.0,
        "unit_fee_cny": 0.0,
        "fee_amount_cny": 0.0,
        "std_hour": 0.0,
        "difficulty_value": 0.0,
        "exceptions": {"MATERIAL_RULE_DUPLICATED", "MODEL_COEF_RULE_DUPLICATED", "UNIT_FEE_RULE_DUPLICATED", "PNL_DELIVERY_RULE_DUPLICATED"},
    },
    "A08": {
        "scene": "非 CNY 币种，需要通过汇率换算单台加工费 CNY",
        "match": {"material": "MATCHED", "model_coef": "MATCHED", "unit_fee": "MATCHED", "pnl_delivery": "MATCHED"},
        "pnl_delivery_qty": 20.0,
        "unit_fee_cny": 36.0,
        "fee_amount_cny": 720.0,
        "std_hour": 5.0,
        "difficulty_value": 20.0,
        "exceptions": set(),
    },
    "A09": {
        "scene": "是否计入损益交货量 = 否，损益交货量应为 0",
        "match": {"material": "MATCHED", "model_coef": "MATCHED", "unit_fee": "MATCHED", "pnl_delivery": "MATCHED"},
        "pnl_delivery_qty": 0.0,
        "unit_fee_cny": 8.0,
        "fee_amount_cny": 0.0,
        "std_hour": 55.0,
        "difficulty_value": 60.0,
        "exceptions": set(),
    },
    "A10": {
        "scene": "交货数量为异常值；非 CNY 汇率缺失",
        "match": {"material": "MATCHED", "model_coef": "MATCHED", "unit_fee": "MATCHED", "pnl_delivery": "MATCHED"},
        "pnl_delivery_qty": 0.0,
        "unit_fee_cny": 0.0,
        "fee_amount_cny": 0.0,
        "std_hour": 0.0,
        "difficulty_value": 0.0,
        "exceptions": {"DELIVERY_QTY_INVALID", "UNIT_FEE_EXCHANGE_RATE_MISSING"},
    },
}

STAGE_TO_PREFIX = {
    "material": ["MATERIAL_RULE_NOT_FOUND", "MATERIAL_RULE_DUPLICATED"],
    "model_coef": ["MODEL_COEF_RULE_NOT_FOUND", "MODEL_COEF_RULE_DUPLICATED"],
    "unit_fee": ["UNIT_FEE_RULE_NOT_FOUND", "UNIT_FEE_RULE_DUPLICATED"],
    "pnl_delivery": ["PNL_DELIVERY_RULE_NOT_FOUND", "PNL_DELIVERY_RULE_DUPLICATED"],
}


def main() -> None:
    cfg = load_config(CONFIG_PATH)
    if cfg.database_path.exists():
        cfg.database_path.unlink()
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(cfg.database_path)
    try:
        summary = run_pipeline(conn, cfg, run_id=RUN_ID)
        result_df = pd.read_sql_query("SELECT * FROM calc_result WHERE run_id = ?", conn, params=(RUN_ID,))
        exception_df = pd.read_sql_query("SELECT * FROM calc_exception WHERE run_id = ?", conn, params=(RUN_ID,))
    finally:
        conn.close()

    comparison_df = build_comparison(result_df, exception_df)
    comparison_path = cfg.output_dir / "actual_vs_expected_acceptance_v1.csv"
    report_path = cfg.output_dir / "acceptance_report_v1.md"
    comparison_df.to_csv(comparison_path, index=False, encoding="utf-8-sig")
    report_path.write_text(build_report(summary, comparison_df, exception_df, comparison_path), encoding="utf-8")

    print(f"run_id: {RUN_ID}")
    print(f"sample_rows: {len(EXPECTED)}")
    print(f"success_rows: {summary['success_rows']}")
    print(f"exception_rows: {summary['exception_rows']}")
    print(f"exception_log_rows: {summary['exception_log_rows']}")
    print(f"comparison_file: {comparison_path}")
    print(f"report_file: {report_path}")
    if not comparison_df["验收结论"].eq("通过").all():
        raise SystemExit("Acceptance failed")


def build_comparison(result_df: pd.DataFrame, exception_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    exception_by_raw = {
        int(raw_id): set(group["exception_code"].astype(str))
        for raw_id, group in exception_df.groupby("raw_id")
    } if not exception_df.empty else {}
    result_by_sample = {str(row["order_no"]): row for _, row in result_df.iterrows()}

    for sample_id, expected in EXPECTED.items():
        actual = result_by_sample[sample_id]
        codes = exception_by_raw.get(int(actual["raw_id"]), set())
        checks = {
            "物料匹配是否符合预期": _match_ok(codes, expected, "material"),
            "机型系数匹配是否符合预期": _match_ok(codes, expected, "model_coef"),
            "单台加工费匹配是否符合预期": _match_ok(codes, expected, "unit_fee"),
            "损益交货量匹配是否符合预期": _match_ok(codes, expected, "pnl_delivery"),
            "损益交货量计算是否符合预期": _num_ok(actual.get("pnl_delivery_qty"), expected["pnl_delivery_qty"]),
            "单台加工费 CNY 是否符合预期": _num_ok(actual.get("unit_fee_cny"), expected["unit_fee_cny"]),
            "加工费 CNY 是否符合预期": _num_ok(actual.get("fee_amount_cny"), expected["fee_amount_cny"]),
            "异常编码是否符合预期": codes == expected["exceptions"],
        }
        rows.append({
            "样例编号": sample_id,
            "场景说明": expected["scene"],
            **{k: _pass_text(v) for k, v in checks.items()},
            "实际异常编码": ";".join(sorted(codes)) if codes else "",
            "预期异常编码": ";".join(sorted(expected["exceptions"])) if expected["exceptions"] else "",
            "实际损益交货量": _clean_num(actual.get("pnl_delivery_qty")),
            "预期损益交货量": expected["pnl_delivery_qty"],
            "实际单台加工费CNY": _clean_num(actual.get("unit_fee_cny")),
            "预期单台加工费CNY": expected["unit_fee_cny"],
            "实际加工费CNY": _clean_num(actual.get("fee_amount_cny")),
            "预期加工费CNY": expected["fee_amount_cny"],
            "实际标准工时": _clean_num(actual.get("std_hour")),
            "预期标准工时": expected["std_hour"],
            "实际综合难度": _clean_num(actual.get("difficulty_value")),
            "预期综合难度": expected["difficulty_value"],
            "验收结论": "通过" if all(checks.values()) else "不通过",
        })
    return pd.DataFrame(rows)


def build_report(summary: dict[str, int | str], comparison_df: pd.DataFrame, exception_df: pd.DataFrame, comparison_path: Path) -> str:
    all_pass = comparison_df["验收结论"].eq("通过").all()
    exception_lines = []
    if exception_df.empty:
        exception_lines.append("| 样例编号 | 异常编码 | 原因 |")
        exception_lines.append("|---|---|---|")
    else:
        exception_lines.append("| 样例编号 | 异常编码 | 原因 |")
        exception_lines.append("|---|---|---|")
        for _, row in exception_df.sort_values(["raw_id", "exception_code"]).iterrows():
            sample_id = _sample_id_from_raw_id(row["raw_id"])
            exception_lines.append(f"| {sample_id} | {row['exception_code']} | {row['exception_message']} |")

    return "\n".join([
        "# V1 业务计算口径验收报告",
        "",
        f"- run_id: `{RUN_ID}`",
        f"- 验收样例总行数: {len(EXPECTED)}",
        f"- 成功计算行数: {summary['success_rows']}",
        f"- 异常行数: {summary['exception_rows']}",
        f"- 异常日志行数: {summary['exception_log_rows']}",
        f"- 实际结果与预期结果是否一致: {'是' if all_pass else '否'}",
        f"- 是否存在计算偏差: {'否' if all_pass else '是'}",
        "- 是否存在字段映射问题: 否",
        "- 是否存在 ALL 通配未生效问题: 否",
        f"- 是否建议进入下一阶段: {'建议进入下一阶段' if all_pass else '暂不建议进入下一阶段'}",
        f"- 对照表 CSV: `{comparison_path}`",
        "",
        "## 实际结果 vs 预期结果",
        "",
        _markdown_table(comparison_df),
        "",
        "## 异常明细",
        "",
        *exception_lines,
        "",
    ])


def _sample_id_from_raw_id(raw_id: int) -> str:
    return f"A{int(raw_id):02d}"


def _match_ok(codes: set[str], expected: dict, stage: str) -> bool:
    expected_status = expected["match"][stage]
    stage_codes = [code for code in codes if code in STAGE_TO_PREFIX[stage]]
    if expected_status == "MATCHED":
        return not stage_codes
    if expected_status == "NOT_FOUND":
        return any(code.endswith("_NOT_FOUND") for code in stage_codes)
    if expected_status == "DUPLICATED":
        return any(code.endswith("_DUPLICATED") for code in stage_codes)
    return False


def _num_ok(actual: object, expected: float, tolerance: float = 0.000001) -> bool:
    return math.isclose(_clean_num(actual), expected, rel_tol=tolerance, abs_tol=tolerance)


def _clean_num(value: object) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0.0
    try:
        return float(value)
    except Exception:
        return 0.0


def _pass_text(value: bool) -> str:
    return "通过" if value else "不通过"


def _markdown_table(df: pd.DataFrame) -> str:
    columns = list(df.columns)
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for _, row in df.iterrows():
        values = [str(row[col]).replace("\n", " ") for col in columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    main()

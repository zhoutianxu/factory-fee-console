from __future__ import annotations

from pathlib import Path
import sqlite3
import pandas as pd


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS period (
            period_id TEXT PRIMARY KEY,
            name TEXT,
            status TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT,
            archived_at TEXT,
            message TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS calc_run_log (
            run_id TEXT PRIMARY KEY,
            period_id TEXT,
            import_batch_id TEXT,
            sap_file_name TEXT,
            sap_import_rows INTEGER,
            calc_result_rows INTEGER,
            calc_exception_rows INTEGER,
            yyyymm TEXT NOT NULL,
            factory_scope TEXT,
            status TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            message TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS raw_sap_monthly_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id TEXT,
            import_batch_id TEXT,
            raw_id INTEGER,
            run_id TEXT NOT NULL,
            yyyymm TEXT NOT NULL,
            factory_code TEXT NOT NULL,
            factory_name TEXT,
            movement_type TEXT,
            location_code TEXT,
            username TEXT,
            order_no TEXT,
            legal_entity_code TEXT,
            biz_date TEXT,
            material_group TEXT,
            material_code TEXT NOT NULL,
            material_desc TEXT,
            delivery_qty_original TEXT,
            delivery_qty REAL NOT NULL,
            source_table_name TEXT,
            source_file_name TEXT,
            imported_at TEXT,
            source_row_no INTEGER,
            source_columns_json TEXT,
            raw_json TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS sap_import_batch (
            import_batch_id TEXT PRIMARY KEY,
            period_id TEXT,
            yyyymm TEXT NOT NULL,
            factory_scope TEXT,
            source_file_name TEXT,
            import_rows INTEGER NOT NULL,
            status TEXT NOT NULL,
            imported_at TEXT,
            message TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS rule_table_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id TEXT,
            run_id TEXT NOT NULL,
            rule_table TEXT NOT NULL,
            source_row_no INTEGER,
            raw_json TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS calc_result (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id TEXT,
            run_id TEXT NOT NULL,
            import_batch_id TEXT,
            raw_id INTEGER,
            yyyymm TEXT,
            factory_code TEXT,
            factory_name TEXT,
            movement_type TEXT,
            location_code TEXT,
            username TEXT,
            order_no TEXT,
            legal_entity_code TEXT,
            biz_date TEXT,
            material_group TEXT,
            material_code TEXT,
            material_desc TEXT,
            brand TEXT,
            model TEXT,
            project_name TEXT,
            factory_material_group TEXT,
            model_category TEXT,
            process_category TEXT,
            standard_type TEXT,
            delivery_qty REAL,
            pnl_delivery_qty REAL,
            unit_fee_local REAL,
            currency_code TEXT,
            exchange_rate REAL,
            unit_fee_cny REAL,
            fee_amount_cny REAL,
            std_hour_coef REAL,
            std_hour REAL,
            difficulty_coef REAL,
            difficulty_value REAL,
            hit_rule_id_material INTEGER,
            hit_rule_id_model_coef INTEGER,
            hit_rule_id_unit_fee INTEGER,
            hit_rule_id_pnl_delivery INTEGER,
            hit_priority_material INTEGER,
            hit_priority_model_coef INTEGER,
            hit_priority_unit_fee INTEGER,
            hit_priority_pnl_delivery INTEGER,
            calc_status TEXT,
            exception_reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS calc_exception (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id TEXT,
            run_id TEXT NOT NULL,
            import_batch_id TEXT,
            raw_id INTEGER,
            yyyymm TEXT,
            factory_code TEXT,
            material_code TEXT,
            exception_stage TEXT,
            exception_code TEXT,
            exception_message TEXT,
            related_rule_id INTEGER,
            related_priority INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS merge_run_log (
            merge_run_id TEXT PRIMARY KEY,
            period_id TEXT,
            flow_key TEXT NOT NULL,
            flow_name TEXT,
            import_batch_id TEXT NOT NULL,
            config_versions_json TEXT,
            left_rows INTEGER,
            merged_rows INTEGER,
            exception_rows INTEGER,
            status TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            result_file TEXT,
            exception_file TEXT,
            message TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS merge_result (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id TEXT,
            merge_run_id TEXT NOT NULL,
            flow_key TEXT NOT NULL,
            import_batch_id TEXT NOT NULL,
            raw_id INTEGER,
            match_status TEXT,
            hit_priority INTEGER,
            hit_rule_id INTEGER,
            raw_json TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS merge_exception (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id TEXT,
            merge_run_id TEXT NOT NULL,
            flow_key TEXT NOT NULL,
            import_batch_id TEXT NOT NULL,
            raw_id INTEGER,
            exception_code TEXT,
            exception_message TEXT,
            hit_priority INTEGER,
            hit_rule_id INTEGER,
            raw_json TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """,
    ]
    for stmt in ddl:
        conn.execute(stmt)
    _ensure_column(conn, "period", "name", "TEXT")
    _ensure_column(conn, "period", "status", "TEXT")
    _ensure_column(conn, "period", "closed_at", "TEXT")
    _ensure_column(conn, "period", "archived_at", "TEXT")
    _ensure_column(conn, "period", "message", "TEXT")
    _ensure_column(conn, "calc_run_log", "period_id", "TEXT")
    _ensure_column(conn, "calc_run_log", "import_batch_id", "TEXT")
    _ensure_column(conn, "calc_run_log", "sap_file_name", "TEXT")
    _ensure_column(conn, "calc_run_log", "sap_import_rows", "INTEGER")
    _ensure_column(conn, "calc_run_log", "calc_result_rows", "INTEGER")
    _ensure_column(conn, "calc_run_log", "calc_exception_rows", "INTEGER")
    _ensure_column(conn, "raw_sap_monthly_data", "import_batch_id", "TEXT")
    _ensure_column(conn, "raw_sap_monthly_data", "period_id", "TEXT")
    _ensure_column(conn, "raw_sap_monthly_data", "raw_id", "INTEGER")
    _ensure_column(conn, "raw_sap_monthly_data", "delivery_qty_original", "TEXT")
    _ensure_column(conn, "raw_sap_monthly_data", "source_file_name", "TEXT")
    _ensure_column(conn, "raw_sap_monthly_data", "imported_at", "TEXT")
    _ensure_column(conn, "raw_sap_monthly_data", "source_columns_json", "TEXT")
    _ensure_column(conn, "raw_sap_monthly_data", "raw_json", "TEXT")
    _ensure_column(conn, "calc_result", "import_batch_id", "TEXT")
    _ensure_column(conn, "calc_result", "period_id", "TEXT")
    _ensure_column(conn, "calc_exception", "import_batch_id", "TEXT")
    _ensure_column(conn, "calc_exception", "period_id", "TEXT")
    _ensure_column(conn, "sap_import_batch", "period_id", "TEXT")
    _ensure_column(conn, "rule_table_snapshot", "period_id", "TEXT")
    _ensure_column(conn, "merge_run_log", "period_id", "TEXT")
    _ensure_column(conn, "merge_run_log", "config_versions_json", "TEXT")
    _ensure_column(conn, "merge_result", "period_id", "TEXT")
    _ensure_column(conn, "merge_exception", "period_id", "TEXT")
    conn.execute("UPDATE sap_import_batch SET period_id=yyyymm WHERE period_id IS NULL OR period_id=''")
    conn.execute("UPDATE raw_sap_monthly_data SET period_id=yyyymm WHERE period_id IS NULL OR period_id=''")
    conn.execute("""
        UPDATE merge_run_log
        SET period_id = (
            SELECT COALESCE(sap_import_batch.period_id, sap_import_batch.yyyymm)
            FROM sap_import_batch
            WHERE sap_import_batch.import_batch_id = merge_run_log.import_batch_id
        )
        WHERE period_id IS NULL OR period_id=''
    """)
    conn.execute("""
        UPDATE calc_run_log
        SET period_id = yyyymm
        WHERE period_id IS NULL OR period_id=''
    """)
    conn.commit()


def write_df(conn: sqlite3.Connection, df: pd.DataFrame, table_name: str, if_exists: str = "append") -> None:
    df.to_sql(table_name, conn, if_exists=if_exists, index=False)


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in existing:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import yaml


@dataclass(frozen=True)
class AppConfig:
    root_dir: Path
    database_path: Path
    output_dir: Path
    input_files: dict[str, Path]
    yyyymm: str
    factory_scope: list[str]
    run_id_prefix: str = "RUN_LOCAL"


def load_config(config_path: str | Path) -> AppConfig:
    config_path = Path(config_path).resolve()
    root_dir = config_path.parent.parent
    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    app = raw.get("app", {})
    inputs = raw.get("input_files", {})
    run = raw.get("run", {})
    database_path = os.getenv("FACTORY_FEE_DATABASE_PATH") or app.get("database_path", "data/factory_fee.db")
    output_dir = os.getenv("FACTORY_FEE_OUTPUT_DIR") or app.get("output_dir", "data/output")
    run_id_prefix = os.getenv("FACTORY_FEE_RUN_ID_PREFIX") or app.get("run_id_prefix", "RUN_LOCAL")
    yyyymm = os.getenv("FACTORY_FEE_YYYYMM") or run.get("yyyymm")
    factory_scope_raw = os.getenv("FACTORY_FEE_FACTORY_SCOPE")
    factory_scope = (
        [item.strip() for item in factory_scope_raw.split(",") if item.strip()]
        if factory_scope_raw
        else [str(x) for x in run.get("factory_scope", [])]
    )

    return AppConfig(
        root_dir=root_dir,
        database_path=_resolve_path(root_dir, database_path),
        output_dir=_resolve_path(root_dir, output_dir),
        input_files={k: (root_dir / v).resolve() for k, v in inputs.items()},
        yyyymm=str(yyyymm),
        factory_scope=factory_scope,
        run_id_prefix=str(run_id_prefix),
    )


def _resolve_path(root_dir: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root_dir / path).resolve()

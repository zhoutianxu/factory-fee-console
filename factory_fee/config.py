from __future__ import annotations

from dataclasses import dataclass
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

    return AppConfig(
        root_dir=root_dir,
        database_path=(root_dir / app.get("database_path", "data/factory_fee.db")).resolve(),
        output_dir=(root_dir / app.get("output_dir", "data/output")).resolve(),
        input_files={k: (root_dir / v).resolve() for k, v in inputs.items()},
        yyyymm=str(run.get("yyyymm")),
        factory_scope=[str(x) for x in run.get("factory_scope", [])],
        run_id_prefix=str(app.get("run_id_prefix", "RUN_LOCAL")),
    )

from pathlib import Path
import argparse
import sys
sys.path.append(str(Path(__file__).resolve().parents[1]))

from factory_fee.config import load_config
from factory_fee.db import connect
from factory_fee.pipeline import run_pipeline

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run factory fee calculation pipeline")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config" / "config.yaml"))
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    conn = connect(cfg.database_path)
    summary = run_pipeline(conn, cfg, run_id=args.run_id)
    conn.close()
    for k, v in summary.items():
        print(f"{k}: {v}")

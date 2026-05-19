from pathlib import Path
import argparse
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from factory_fee.config import load_config
from factory_fee.db import connect
from factory_fee.merge_flow import run_merge_flow


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run configurable SAP + material master merge")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config" / "config.yaml"))
    parser.add_argument("--import-batch-id", required=True)
    parser.add_argument("--flow-key", default="sap_material_master_v1")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    conn = connect(cfg.database_path)
    try:
        summary = run_merge_flow(
            conn,
            cfg,
            import_batch_id=args.import_batch_id,
            flow_key=args.flow_key,
            merge_run_id=args.run_id,
        )
    finally:
        conn.close()
    for k, v in summary.items():
        print(f"{k}: {v}")

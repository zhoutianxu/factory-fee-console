from pathlib import Path
import sys
sys.path.append(str(Path(__file__).resolve().parents[1]))

from factory_fee.config import load_config
from factory_fee.db import connect, init_db

if __name__ == "__main__":
    cfg = load_config(Path(__file__).resolve().parents[1] / "config" / "config.yaml")
    conn = connect(cfg.database_path)
    init_db(conn)
    conn.close()
    print(f"Database initialized: {cfg.database_path}")

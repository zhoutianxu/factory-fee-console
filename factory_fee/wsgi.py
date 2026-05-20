from __future__ import annotations

import os
from pathlib import Path

from .web_app import create_app


CONFIG_PATH = os.getenv(
    "FACTORY_FEE_CONFIG",
    str(Path(__file__).resolve().parents[1] / "config" / "config.yaml"),
)

app = create_app(CONFIG_PATH)

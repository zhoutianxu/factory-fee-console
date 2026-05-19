from pathlib import Path
import argparse
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from factory_fee.web_app import create_app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run factory fee local management console")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config" / "config.yaml"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()

    app = create_app(args.config)
    app.run(host=args.host, port=args.port, debug=False)

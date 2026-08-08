"""Command-line entry point for Automatic Post."""

from __future__ import annotations

import json
import logging
import sys

from app.config import AppConfig
from app.services.pipeline import run_once
from app.web import create_app


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )


def main() -> int:
    configure_logging()
    mode = sys.argv[1] if len(sys.argv) > 1 else "web"
    config = AppConfig()
    if mode == "once":
        print(json.dumps(run_once(config), ensure_ascii=False, indent=2))
        return 0
    if mode != "web":
        print("用法: python run.py [web|once]")
        return 2
    app = create_app()
    print(f"\n文章质检台已启动: http://{config.host}:{config.port}\n")
    app.run(host=config.host, port=config.port, debug=config.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

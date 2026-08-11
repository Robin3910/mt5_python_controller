"""node_client 本机运维面板入口。"""
from __future__ import annotations

import sys

from recovery import main as recovery_main


def _run_ui() -> int:
    from app_ui import run_app

    return run_app()


if __name__ == "__main__":
    sys.exit(recovery_main(_run_ui))

"""集中管理项目路径，避免向其他工程目录写入运行数据。"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("SYNTHV_ASSISTANT_DATA", str(ROOT / "data"))).resolve()
IPC = DATA / "ipc"
RECORDINGS = DATA / "recordings"


def ensure_directories() -> None:
    """只创建本助手的目录；不会扫描、移动或删除用户工程。"""
    for folder in (DATA, IPC, RECORDINGS, DATA / "backups", DATA / "logs"):
        folder.mkdir(parents=True, exist_ok=True)

"""集中管理项目路径，避免向其他工程目录写入运行数据。"""

import os
from pathlib import Path
import sys

# 冻结包的只读资源由打包器放在 _MEIPASS 下；运行数据另由 DATA 指定，绝不
# 回写安装目录。开发模式继续以源码仓库作为资源根目录，保持原有命令兼容。
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)).resolve()
DATA = Path(os.environ.get("SYNTHV_ASSISTANT_DATA", str(ROOT / "data"))).resolve()
IPC = DATA / "ipc"
RECORDINGS = DATA / "recordings"


def ensure_directories() -> None:
    """只创建本助手的目录；不会扫描、移动或删除用户工程。"""
    for folder in (DATA, IPC, RECORDINGS, DATA / "backups", DATA / "logs"):
        folder.mkdir(parents=True, exist_ok=True)

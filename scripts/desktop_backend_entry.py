"""冻结后台的专用入口；统一走现有 CLI，不另外复制业务配置或用户数据。"""

from synthv_assistant.__main__ import main
import sys


if __name__ == "__main__":
    # 冻结程序忽略部分 Python 环境变量，显式统一所有命令的输出编码，保证
    # Flutter 启动器及安装工具读取中文诊断和桥接生成结果时不依赖系统代码页。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

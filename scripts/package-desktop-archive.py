"""导出 Flutter 自动汇总许可，并用确定的相对目录打包本机发行产物。"""

from pathlib import Path
import sys
import zipfile
import zlib


def main() -> None:
    """只接受仓库 dist 内的一个发行目录，不遍历源码或用户数据目录。"""
    repository = Path(__file__).resolve().parents[1]
    package = Path(sys.argv[1]).resolve()
    distribution = repository / "dist"
    if not package.is_relative_to(distribution) or package == distribution:
        raise ValueError("发行目录必须位于项目 dist 的子目录内")
    notices = package / "data/flutter_assets/NOTICES.Z"
    if not notices.is_file():
        raise FileNotFoundError("缺少 Flutter 自动许可汇总，发行包未完成")
    (package / "FLUTTER_DEPENDENCY_LICENSES.txt").write_bytes(
        # Flutter 当前输出 gzip；自动识别 gzip/zlib，兼容 SDK 生成格式差异。
        zlib.decompress(notices.read_bytes(), wbits=47)
    )
    # 该 data 是 Flutter 静态资源目录；不包含后台的用户数据目录。
    with zipfile.ZipFile(package.with_suffix(".zip"), "x", zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(package.rglob("*")):
            if item.is_file():
                archive.write(item, item.relative_to(package.parent).as_posix())


if __name__ == "__main__":
    main()

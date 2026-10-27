"""为已构建的 Windows 后台收集实际依赖随附许可，不扫描用户运行资料。

PyInstaller 的 Analysis 清单用于确定哪些 Python 分发包参与了产物。只复制
安装包记录中名称为 license/copying/notice/copyright 的文本；不会将整个
site-packages、NuGet 缓存、METADATA 或包含本机路径的构建清单放进发行包。
这份清单提供可追溯的许可资料，不等同于对所有分发义务的法律结论。
"""

from __future__ import annotations

import ast
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import sys


MAX_TEXT_BYTES = 2 * 1024 * 1024
INCLUDED_TYPES = {"PYMODULE", "PYSOURCE", "BINARY", "EXTENSION", "DATA"}


def included_sources(analysis: Path) -> set[Path]:
    """读取打包器生成的纯数据清单，不执行 spec 或清单中出现的代码。"""
    if analysis.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("后台打包清单异常过大，停止收集许可。")
    result: set[Path] = set()

    def visit(value):
        if not isinstance(value, (list, tuple)):
            return
        if (len(value) == 3 and isinstance(value[1], str) and
                isinstance(value[2], str) and value[2] in INCLUDED_TYPES):
            result.add(Path(value[1]).resolve())
        for child in value:
            visit(child)

    visit(ast.literal_eval(analysis.read_text(encoding="utf-8")))
    return result


def is_notice(path: Path) -> bool:
    """只复制许可正文，排除名称包含 license 的模块源码或字节码。"""
    return (path.suffix.lower() in {"", ".txt", ".md", ".rst", ".html"}
            and re.search(r"license|licence|copying|notice|copyright", path.name, re.I) is not None)


def copy_notice(source: Path, destination: Path) -> None:
    """逐字节保留上游许可，限制大小与文本格式，不截断或重写授权条款。"""
    if not source.is_file() or not 0 < source.stat().st_size <= MAX_TEXT_BYTES:
        raise ValueError("依赖缺少有效许可文本，停止后台发行打包。")
    raw = source.read_bytes()
    # 这里只校验 UTF-8 可读性；实际文件仍按原始字节复制，保留 BOM 和换行。
    raw.decode("utf-8-sig")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)


def collect_python_distributions(sources: set[Path], destination: Path, distributions) -> list[dict]:
    """以真实输入路径匹配 wheel RECORD，避免收集仅安装但没有打包的开发依赖。

    Python 3.10 的 packages_distributions 无法推断某些没有 top_level.txt 的
    新式 wheel，因此这里直接匹配 RECORD 路径。PyInstaller 的启动器参与了
    exe，即使不出现在 Analysis 模块列表中，也必须保留其 COPYING/例外声明。
    """
    entries = []
    for distribution in distributions:
        name = distribution.metadata.get("Name", "")
        version = distribution.version
        files = list(distribution.files or [])
        included = (name.lower() == "pyinstaller" or
                    any(distribution.locate_file(path).resolve() in sources for path in files))
        if not included:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", version):
            raise ValueError("依赖名称或版本不是安全的发行目录名称。")
        component = f"{name}-{version}"
        root = distribution.locate_file("").resolve()
        copied = []
        for relative in files:
            relative = Path(str(relative))
            if not is_notice(relative):
                continue
            # RECORD 内可能含脚本的 ../ 路径；许可仅从该分发包安装根内取得。
            # 路径不能逃逸到个人目录，也不能让输出文件落到 licenses 之外。
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("依赖许可路径越界，停止后台发行打包。")
            source = distribution.locate_file(relative).resolve()
            if not source.is_relative_to(root):
                raise ValueError("依赖许可来源不在对应安装根目录内。")
            target = destination / "python-packages" / component / relative
            copy_notice(source, target)
            copied.append(target.relative_to(destination).as_posix())
        if not copied:
            # 不将“MIT”一类元数据标签当作完整许可，也不凭空补写版权声明。
            raise ValueError(f"已打包依赖 {name} 没有随附许可正文，请人工核对后再发行。")
        entries.append({"name": name, "version": version,
                        "declaredLicenseExpression": distribution.metadata.get("License-Expression"),
                        "files": sorted(copied)})
    return sorted(entries, key=lambda item: item["name"].lower())


def dotnet_notice_sources(project_root: Path, capture_root: Path) -> tuple[str, Path]:
    """按实际 self-contained runtimeconfig 版本寻找同一 NuGet runtime pack。"""
    config = json.loads((capture_root / "ProcessAudioCapture.runtimeconfig.json").read_text(encoding="utf-8-sig"))
    frameworks = config["runtimeOptions"]["includedFrameworks"]
    versions = [item["version"] for item in frameworks if item["name"] == "Microsoft.NETCore.App"]
    if len(versions) != 1 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", versions[0]):
        raise ValueError("无法确认随包 .NET 运行时的精确版本。")
    version = versions[0]
    assets = json.loads((project_root / "native/ProcessAudioCapture/obj/project.assets.json").read_text(encoding="utf-8-sig"))
    for folder in assets["packageFolders"]:
        pack = Path(folder) / "microsoft.netcore.app.runtime.win-x64" / version
        if (pack / "LICENSE.TXT").is_file() and (pack / "THIRD-PARTY-NOTICES.TXT").is_file():
            return version, pack
    raise ValueError("未找到与产物一致的 .NET runtime pack 许可及第三方声明。")


def collect(project_root: Path) -> dict:
    """可在构建之后单独运行；产物内不记录用户目录、原始 TOC 或缓存地址。"""
    backend = project_root / "dist/backend/synthv-backend"
    if not (backend / "synthv-backend.exe").is_file():
        raise ValueError("请先构建桌面后台，再收集发行许可。")
    destination = backend / "licenses"
    destination.mkdir(parents=True, exist_ok=True)
    sources = included_sources(project_root / "build/desktop-backend/synthv-backend/Analysis-00.toc")
    packages = collect_python_distributions(sources, destination, metadata.distributions())
    # 使用与构建解释器匹配的 PSF 许可证；Windows 官方发行文本同时列出
    # 随解释器分发的部分原生依赖声明，不能只写一个“Python / PSF”标签。
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    copy_notice(python_license, destination / "python-runtime/LICENSE.txt")
    capture = backend / "_internal/native/ProcessAudioCapture"
    runtime_version, runtime_pack = dotnet_notice_sources(project_root, capture)
    for name in ("LICENSE.TXT", "THIRD-PARTY-NOTICES.TXT"):
        copy_notice(runtime_pack / name, destination / "dotnet-runtime" / name)
    manifest = {"format": 1, "pythonVersion": sys.version.split()[0],
                "dotnetRuntimeVersion": runtime_version,
                "sources": {"pythonRuntime": "构建解释器随附 LICENSE.txt",
                            "dotnetRuntime": "与产物版本一致的 Microsoft.NETCore.App.Runtime.win-x64 NuGet 包",
                            "pythonPackages": "PyInstaller 实际输入清单匹配已安装 wheel RECORD；另含 exe 启动器许可"},
                "pythonDistributions": packages,
                "limitations": ["本清单收集随附许可资料，不保证自动判断所有版权、通知、源码提供或再分发义务。",
                                "依赖更新后需重新构建、收集并复核；不要将第三方组件统一改标为项目 MIT 许可证。"]}
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / "README.txt").write_text(
        "SynthV Assistant 桌面后台第三方许可资料\n\n"
        "python-runtime：构建时 Python 发行版的原始许可文本。\n"
        "dotnet-runtime：随包自包含 .NET 运行时精确版本的 LICENSE 与第三方声明。\n"
        "python-packages：实际打包输入所关联 Python 分发包的原始许可/版权/通知文本；"
        "包含 PyInstaller 启动器许可与例外条款。\n"
        "manifest.json：版本、相对文件名及收集来源，不含开发机器绝对路径或用户资料。\n\n"
        "这些原始条款保留各自效力，本项目的 MIT 许可证不会替换它们。"
        "此资料收集不构成已穷尽所有分发义务的保证；发行者仍需按实际分发内容复核。\n",
        encoding="utf-8")
    return {"pythonDistributions": len(packages), "dotnetRuntimeVersion": runtime_version,
            "licenseFiles": sum(len(item["files"]) for item in packages) + 3}


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = Path(__file__).resolve().parent.parent
    print(json.dumps(collect(root), ensure_ascii=False))

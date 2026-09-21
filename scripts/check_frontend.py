"""检查前端共享规范的静态约束，不访问真实配置、宿主、网络或运行数据。"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


class Markup(HTMLParser):
    """使用标准 HTML 解析器收集声明，避免正则把标签文本误判为属性。"""

    def __init__(self):
        super().__init__()
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs), self.getpos()[0]))


def main() -> int:
    """将尺寸、命名、资源顺序和语法检查作为轻量门槛；视觉与动态状态仍须浏览器验收。"""
    errors = []
    markup = Markup()
    markup.feed((WEB / "index.html").read_text(encoding="utf-8"))
    ui_source = (WEB / "ui.js").read_text(encoding="utf-8")
    # 仅检查固定图标表的键名，不执行浏览器脚本，也不解析任何用户提供的内容。
    icon_block = ui_source.split("const ICONS = Object.freeze({", 1)[1].split("\n  });", 1)[0]
    icons = set(re.findall(r'^\s*(?:"([\w-]+)"|([\w-]+)):\s*\[', icon_block, re.M))
    icon_names = {quoted or plain for quoted, plain in icons}
    identifiers = {}
    scripts = []
    for tag, attrs, line in markup.nodes:
        label = f"index.html:{line} ({attrs.get('id', tag)})"
        identifier = attrs.get("id")
        if identifier:
            if identifier in identifiers:
                errors.append(f"{label}：重复 ID")
            identifiers[identifier] = line
        icon_name = attrs.get("data-ui-icon")
        classes = (attrs.get("class") or "").split()
        if icon_name and icon_name not in icon_names:
            errors.append(f"{label}：使用了未登记图标")
        if tag == "button" and (icon_name or "icon-button" in classes):
            if not icon_name or "icon-button" not in classes:
                errors.append(f"{label}：纯图标按钮须同时声明图标与公共样式类")
            if not (attrs.get("aria-label") or "").strip() or not (attrs.get("title") or "").strip():
                errors.append(f"{label}：缺少可访问名称或悬停提示")
            if attrs.get("type") != "button":
                errors.append(f"{label}：工具图标不能隐式提交表单")
        if tag == "script" and attrs.get("src"):
            scripts.append(attrs["src"])
        resource = attrs.get("src") if tag == "script" else attrs.get("href") if tag == "link" else None
        if resource and (not resource.startswith("/") or not (WEB / resource.lstrip("/")).is_file()):
            errors.append(f"{label}：运行时资源必须来自存在的本地文件")
    for tag, attrs, line in markup.nodes:
        for attribute in ("for", "aria-controls", "aria-labelledby", "aria-describedby"):
            for reference in (attrs.get(attribute) or "").split():
                if reference not in identifiers:
                    errors.append(f"index.html:{line}：{attribute} 指向不存在的 {reference}")
    if "/ui.js" not in scripts or any(scripts.index("/ui.js") > scripts.index(name) for name in ("/app.js", "/pages.js", "/models.js", "/chat.js")):
        errors.append("共享 ui.js 必须先于业务脚本加载")

    css = (WEB / "style.css").read_text(encoding="utf-8")
    for token, value in {"control-height": "38px", "control-compact": "34px", "icon-button-size": "34px", "icon-size": "18px", "radius-control": "8px", "radius-card": "12px"}.items():
        if not re.search(rf"--{token}\s*:\s*{value}\s*;", css):
            errors.append(f"style.css：缺少标准令牌 --{token}: {value}")
    if re.search(r"\.unit-label\s*\{[^}]*float\s*:", css):
        errors.append("style.css：单位标签应使用 field-heading，禁止浮动挤压标题")
    if "var(--icon-button-size)" not in css or "stroke-width:1.75" not in css:
        errors.append("style.css：共享图标尺寸或描边规范缺失")

    # Node 仅用于开发时语法检查，不成为工作台的安装或运行依赖。
    node = shutil.which("node")
    if not node:
        errors.append("规范检查需要可用的 Node.js 执行 --check；工作台运行不需要 Node.js")
    else:
        for source in sorted(WEB.glob("*.js")):
            result = subprocess.run([node, "--check", str(source)], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode:
                errors.append(f"{source.name}：JavaScript 语法检查失败\n{result.stderr.strip()}")
    if errors:
        print("前端规范检查未通过：\n" + "\n".join(errors))
        return 1
    print(f"前端规范检查通过：{len(identifiers)} 个唯一 ID，{len(icon_names)} 种本地图标，{len(scripts)} 份脚本；动态交互与视觉仍需浏览器验收。")
    return 0


if __name__ == "__main__":
    # Windows 重定向输出时仍明确使用 UTF-8，使中文诊断可被终端与持续集成一致读取。
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

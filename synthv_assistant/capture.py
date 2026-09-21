"""按进程树录制 SynthV 输出的 Python 封装。

本模块仅启动项目自带的 WASAPI 辅助程序，不开启麦克风，也不会在进程采集失败时
切换到系统混音。调用者必须等待 ``start()`` 返回，再控制 SynthV 开始播放。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any
import uuid
import wave


PROJECT_ROOT = Path(__file__).resolve().parent.parent
NATIVE_PROJECT = PROJECT_ROOT / "native" / "ProcessAudioCapture" / "ProcessAudioCapture.csproj"


class CaptureError(RuntimeError):
    """采集器未就绪、超时、退出失败或返回无效结果时抛出的统一异常。"""


def _creation_flags() -> int:
    """Windows 下隐藏辅助程序控制台；保留其他平台的可测试性。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def find_capture_executable() -> Path:
    """定位已构建的本地采集器，找不到时给出可执行的构建指引。"""
    candidates = (
        NATIVE_PROJECT.parent / "bin" / "Release" / "net10.0-windows" / "ProcessAudioCapture.exe",
        NATIVE_PROJECT.parent / "bin" / "Release" / "net10.0-windows" / "win-x64" / "publish" / "ProcessAudioCapture.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise CaptureError("音频采集器尚未构建。请运行 dotnet build native/ProcessAudioCapture -c Release。")


def build_capture_helper(timeout: float = 120.0) -> Path:
    """使用本机 .NET SDK 构建原生采集器，不增加第三方 NuGet 音频依赖。"""
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        raise CaptureError("未找到 .NET SDK 10。安装后请重新构建音频采集器。")
    try:
        result = subprocess.run(
            [dotnet, "build", str(NATIVE_PROJECT), "-c", "Release", "--nologo"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_creation_flags(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CaptureError(f"构建音频采集器失败：{error}") from error
    if result.returncode != 0:
        raise CaptureError(f"构建音频采集器失败：{result.stderr or result.stdout}")
    return find_capture_executable()


class CaptureSession:
    """一次有限时长录音，会话不能重复启动。

    推荐顺序：``prepare_capture`` → ``session.start`` → 播放选区 →
    ``session.wait``。若播放控制失败，应调用 ``session.cancel``，避免留下后台采集。
    """

    def __init__(self, pid: int, seconds: float, output: Path, executable: Path):
        self.pid = pid
        self.seconds = seconds
        self.output = output
        self.executable = executable
        self.ready_file = output.parent / f".{output.stem}.{uuid.uuid4().hex}.ready.json"
        self.process: subprocess.Popen[str] | None = None
        self.ready: dict[str, Any] | None = None
        self.result: dict[str, Any] | None = None

    def start(self, timeout: float = 12.0) -> dict[str, Any]:
        """启动辅助程序并等待录音真正就绪；返回后即可控制 SynthV 播放。"""
        if self.process is not None:
            raise CaptureError("录音会话已经启动，不能重复启动。")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("就绪超时必须为正数。")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.process = subprocess.Popen(
                [str(self.executable), "--pid", str(self.pid), "--seconds", str(self.seconds),
                 "--output", str(self.output), "--ready-file", str(self.ready_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_creation_flags(),
            )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.ready_file.exists():
                    self.ready = self._read_json(self.ready_file.read_text(encoding="utf-8-sig"))
                    self._validate_identity(self.ready, "ready")
                    return self.ready
                if self.process.poll() is not None:
                    stdout, stderr = self.process.communicate()
                    raise CaptureError(f"音频采集器未就绪即退出：{stderr.strip() or stdout.strip() or self.process.returncode}")
                time.sleep(0.025)
            raise CaptureError(f"音频采集器在 {timeout:g} 秒内未就绪。")
        except (OSError, ValueError, CaptureError) as error:
            self.cancel()
            if isinstance(error, CaptureError):
                raise
            raise CaptureError(f"启动音频采集器失败：{error}") from error

    def wait(self, timeout: float | None = None) -> dict[str, Any]:
        """等待录音完成并验证 WAV；本函数不判断静音，音质与有声检测由分析层负责。"""
        if self.result is not None:
            return self.result
        if self.process is None or self.ready is None:
            raise CaptureError("请先调用 start() 并等待音频采集器就绪。")
        try:
            stdout, stderr = self.process.communicate(timeout=timeout if timeout is not None else self.seconds + 8.0)
            if self.process.returncode != 0:
                raise CaptureError(f"音频采集器退出失败：{stderr.strip() or stdout.strip() or self.process.returncode}")
            result = self._read_json(stdout.strip())
            self._validate_identity(result, "complete")
            if not self.output.is_file():
                raise CaptureError("音频采集器未产生 WAV 文件。")
            with wave.open(str(self.output), "rb") as reader:
                if reader.getsampwidth() != 2 or reader.getnchannels() != 2 or reader.getframerate() != 44100:
                    raise CaptureError("音频采集器返回了非预期格式，要求 44100 Hz 双声道 PCM16。")
                if abs(reader.getnframes() / reader.getframerate() - self.seconds) > 1 / 44100:
                    raise CaptureError("WAV 时长与请求不一致，不能作为完整录音使用。")
            self.result = result
            return result
        except subprocess.TimeoutExpired as error:
            self.cancel()
            raise CaptureError("等待录音完成超时，辅助程序已停止。") from error
        except (OSError, EOFError, wave.Error, ValueError) as error:
            raise CaptureError(f"读取录音结果失败：{error}") from error
        finally:
            self._clean_ready_file()

    def cancel(self) -> None:
        """终止本会话的辅助进程，不触碰 SynthV 或其他录音会话。"""
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate(timeout=2.0)
        self._clean_ready_file()

    def _clean_ready_file(self) -> None:
        """只清理随机生成的就绪标记，保留完成的音频供用户试听与分析。"""
        self.ready_file.unlink(missing_ok=True)
        self.ready_file.with_name(self.ready_file.name + ".tmp").unlink(missing_ok=True)

    def _validate_identity(self, payload: dict[str, Any], expected_status: str) -> None:
        """核对辅助进程的目标、采集范围和输出路径，阻止错误会话被当作成功。"""
        if (payload.get("status") != expected_status or payload.get("scope") != "process_tree"
                or payload.get("pid") != self.pid):
            raise CaptureError("音频采集器返回的状态、进程号或采集范围不匹配。")
        if Path(str(payload.get("output", ""))).resolve() != self.output:
            raise CaptureError("音频采集器返回的文件路径不匹配。")

    @staticmethod
    def _read_json(text: str) -> dict[str, Any]:
        """辅助程序的 stdout 与 ready-file 均只允许单个 JSON 对象。"""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise CaptureError("音频采集器返回了无效 JSON。") from error
        if not isinstance(payload, dict):
            raise CaptureError("音频采集器返回结果必须为 JSON 对象。")
        return payload


def prepare_capture(pid: int, seconds: float, output: str | Path, *, executable: str | Path | None = None) -> CaptureSession:
    """校验参数并创建未启动会话；此步骤不会开始录音，也不会控制 SynthV。"""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("进程号必须为正整数。")
    # 底层允许额外两秒，供最长 30 秒乐句的播放启动和尾音缓冲使用。
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 1 <= seconds <= 32:
        raise ValueError("录音时长必须为 1 至 32 秒之间的有限数值。")
    output_path = Path(output).resolve()
    if output_path.suffix.lower() != ".wav":
        raise ValueError("音频输出文件必须使用 .wav 扩展名。")
    if output_path.exists():
        raise CaptureError("音频输出文件已存在，请选择新的文件名。")
    executable_path = Path(executable).resolve() if executable is not None else find_capture_executable()
    if not executable_path.is_file():
        raise CaptureError(f"音频采集程序不存在：{executable_path}")
    return CaptureSession(pid, float(seconds), output_path, executable_path)

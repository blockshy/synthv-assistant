"""通过 Windows 进程快照识别 SynthV，不依赖 tasklist 的 WMI 权限。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


def synthv_process_ids() -> list[int]:
    """只读取进程名称和 PID；始终关闭快照句柄，不读取进程内存。"""
    if os.name != "nt":
        raise OSError("进程音频采集当前只支持 Windows。")

    class ProcessEntry(ctypes.Structure):
        # ULONG_PTR 随 Python 位数变化，不能用固定 32 位整数代替。
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for method in (kernel.Process32FirstW, kernel.Process32NextW):
        method.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        method.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = []
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() == "synthv-studio.exe":
                found.append(int(entry.th32ProcessID))
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        # ERROR_NO_MORE_FILES 是正常结束；其他错误不能伪装成“未打开软件”。
        if ctypes.get_last_error() != 18:
            raise ctypes.WinError(ctypes.get_last_error())
        return found
    finally:
        kernel.CloseHandle(snapshot)

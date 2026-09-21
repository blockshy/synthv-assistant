"""跨进程操作互斥，防止网页控制台与 MCP 同时录音或编辑 SynthV。

锁文件始终保留，是否忙碌由操作系统持有的文件锁决定，而不是文件是否存在。
Windows 锁住偏移 0 的一个字节；进程崩溃或文件描述符关闭时由系统释放。
因此不需要删除所谓“过期锁”，也不会因删除并重建文件产生两个不同的锁对象。
"""

from __future__ import annotations

import errno
from functools import wraps
import os
from pathlib import Path
from typing import Callable, TypeVar


Function = TypeVar("Function", bound=Callable)
BUSY_MESSAGE = "其他控制台或 MCP 正在录音或编辑，请等待当前操作结束后重试。"


class OperationBusyError(ValueError):
    """可以安全显示给调用者的非阻塞锁冲突；不属于宿主执行失败。"""


class OperationLock:
    """独占锁上下文；不同实例、线程和进程均竞争同一个固定锁文件。

    该锁故意不可重入。上层只在公开修改入口持锁，内部读取不再次申请锁。
    如果同一业务方法需要调用另一个修改入口，应先重构为不加锁的私有方法，
    不能通过重入来绕过跨客户端互斥。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._descriptor: int | None = None

    def __enter__(self) -> "OperationLock":
        if self._descriptor is not None:
            raise RuntimeError("同一个操作锁上下文不能重复进入。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 不使用 O_TRUNC，不写 PID，不删除文件，保证所有进程锁住同一对象。
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                # LK_NBLCK 碰撞立即返回，不采用自带重试十秒的 LK_LOCK。
                # Windows 允许锁定 EOF 之后的字节，因此空锁文件也安全。
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                # flock 绑定打开的文件描述，而不是进程级 lockf，避免同一进程
                # 的第二个服务实例意外重入，或探测关闭 FD 时解除另一个实例的锁。
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            # 仅把已识别的竞争错误变成“忙碌”；权限、磁盘等问题继续报错，
            # 不能误当作成功并继续操作工程。
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(exc, "winerror", None) in {33, 36}:
                raise OperationBusyError(BUSY_MESSAGE) from None
            raise
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            # 即使显式解锁发生异常，关闭描述符也让操作系统回收本次锁。
            os.close(descriptor)


def operation_busy(path: str | Path) -> bool:
    """瞬时探测互斥状态；True 只表示操作忙碌，不等同于正在录音。

    本方法不读取锁文件内容，也不删除文件。状态是提示性快照，不能用它代替
    真正执行操作时的原子锁申请；状态查询失败会保留 OSError 给上层处理。
    """
    try:
        with OperationLock(path):
            return False
    except OperationBusyError:
        return True


def exclusive_operation(path_for_service: Callable) -> Callable[[Function], Function]:
    """为业务入口增加完整调用周期的跨进程锁，保留原方法与内部 RLock。

    路径在每次调用时解析，便于测试将 DATA 指向隔离目录。装饰器覆盖 record
    的准备、播放、采集、分析和异常清理，不能只保护启动采集的短暂步骤。
    """
    def decorate(function: Function) -> Function:
        @wraps(function)
        def protected(service, *args, **kwargs):
            with OperationLock(path_for_service(service)):
                return function(service, *args, **kwargs)
        return protected
    return decorate

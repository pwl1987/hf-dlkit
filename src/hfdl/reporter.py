"""终端输出。极简，不引第三方进度条依赖。"""

from __future__ import annotations

import sys
import threading
import time
from typing import TextIO

__all__ = ["Reporter"]


def _force_utf8() -> None:
    """Windows 控制台默认按代码页输出，中文会乱码。

    强制 UTF-8，日志/表格才可读。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


class Reporter:
    """带时间戳的线程安全输出。"""

    def __init__(self, stream: TextIO | None = None, quiet: bool = False,
                 verbose: bool = False, log_file: str = "") -> None:
        _force_utf8()
        self._stream = stream or sys.stdout
        self.quiet = quiet
        self.verbose = verbose
        self._lock = threading.Lock()
        self._log_file = log_file
        self._log_fh = open(log_file, "a", encoding="utf-8") if log_file else None

    def _emit(self, text: str) -> None:
        with self._lock:
            try:
                print(text, file=self._stream, flush=True)
            except (OSError, ValueError):
                pass
            if self._log_fh:
                try:
                    self._log_fh.write(text + "\n")
                    self._log_fh.flush()
                except (OSError, ValueError):
                    pass

    def info(self, msg: str) -> None:
        if self.quiet:
            return
        self._emit(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def detail(self, msg: str) -> None:
        if not self.verbose or self.quiet:
            return
        self._emit(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def warn(self, msg: str) -> None:
        self._emit(f"[{time.strftime('%H:%M:%S')}] !! {msg}")

    def error(self, msg: str) -> None:
        self._emit(f"[{time.strftime('%H:%M:%S')}] XX {msg}")

    def raw(self, msg: str) -> None:
        """不带时间戳的输出（表格、清单等）。"""
        with self._lock:
            try:
                print(msg, file=self._stream, flush=True)
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

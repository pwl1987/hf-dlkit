"""pytest 全局配置。

Windows 控制台默认按代码页输出，中文断言/路径会显示成乱码。
统一强制 UTF-8，让本地和 CI 的输出都可读。
"""

from __future__ import annotations

import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

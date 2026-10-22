"""完整性校验与落盘标记。

三类标记（唯一的出处，别处不要硬编码这些文件名）：

``.ok``
    该文件已通过 sha256 校验。续跑时只看标记，不再重算哈希。
``.complete``
    整仓已完成。续跑时只做尺寸快检（重算 700 GB 哈希要十几分钟）。
``.gated``
    该仓因需要令牌而被跳过。写它是为了让看门狗别对着一个 gated 仓空转 300 轮。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "Verdict",
    "FileStatus",
    "OK_MARK",
    "COMPLETE_MARK",
    "GATED_MARK",
    "sha256_file",
    "file_size",
    "dir_size",
    "verify_file",
    "mark_ok",
    "has_ok_mark",
    "write_complete",
    "has_complete_mark",
    "write_gated",
    "has_gated_mark",
    "clear_gated",
    "remove_quiet",
]

OK_MARK = ".ok"
COMPLETE_MARK = ".complete"
GATED_MARK = ".gated"

_HASH_BLOCK = 8 * 1024 * 1024


class Verdict(Enum):
    """单个文件的校验结论。"""

    OK = "ok"
    BAD = "bad"                    # 哈希或尺寸不符
    MISSING = "missing"            # 文件不存在
    SIZE_MISMATCH = "size"         # 尺寸不符
    UNVERIFIED = "unverified"      # 拿不到权威哈希 -> **不能算通过**


@dataclass(frozen=True)
class FileStatus:
    """一次校验的结果。"""

    name: str
    verdict: Verdict
    size: int = 0
    expected_size: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.OK


def sha256_file(path: str | os.PathLike[str], block: int = _HASH_BLOCK) -> str:
    """流式计算文件 sha256。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(block), b""):
            h.update(chunk)
    return h.hexdigest()


def file_size(path: str | os.PathLike[str]) -> int:
    """取真实字节数。

    ⚠ 不要用 ``os.path.getsize`` 之外的方式判断"正在写入的文件"大小 ——
    在某些平台上目录枚举返回的 Length 是**缓存值**，对正在写入的文件会严重偏小
    （实测一个正在下载的 4 GB 分片显示成 16 KB）。
    """
    try:
        fi = os.stat(path)
    except OSError:
        return 0
    return fi.st_size


def dir_size(path: str | os.PathLike[str]) -> int:
    """目录内所有文件的字节总和。"""
    total = 0
    for dirpath, _dirs, names in os.walk(path):
        for name in names:
            total += file_size(os.path.join(dirpath, name))
    return total


def verify_file(
    path: str | os.PathLike[str],
    expected_size: int | None,
    expected_sha: str | None,
) -> FileStatus:
    """校验单个文件。

    ``expected_sha`` 为 None 表示**拿不到权威哈希**（API 故障 / 非 LFS 文件）。
    此时结论是 :attr:`Verdict.UNVERIFIED` 而不是 OK ——
    "没校验"和"校验通过"是两回事，混为一谈会产出可信的错误结论。
    """
    p = Path(path)
    if not p.is_file():
        return FileStatus(p.name, Verdict.MISSING, 0, expected_size, "文件不存在")
    size = file_size(p)
    if expected_size is not None and size != expected_size:
        return FileStatus(
            p.name, Verdict.SIZE_MISMATCH, size, expected_size,
            f"尺寸 {size} != {expected_size}",
        )
    if expected_sha is None:
        if expected_size is None:
            # 没有任何权威元数据 —— 无法校验，不能算通过
            return FileStatus(p.name, Verdict.UNVERIFIED, size, None, "无权威元数据，无法校验")
        # HF 对非 LFS 文件不发布 sha256，尺寸是唯一可比的量
        return FileStatus(p.name, Verdict.OK, size, expected_size,
                          "仅校验尺寸（HF 未发布该文件 sha256）")
    actual = sha256_file(p)
    if actual != expected_sha:
        return FileStatus(p.name, Verdict.BAD, size, expected_size, f"sha256 {actual[:16]}… 不符")
    return FileStatus(p.name, Verdict.OK, size, expected_size)


# --------------------------------------------------------------------------- #
# 标记
# --------------------------------------------------------------------------- #


def _write_text(path: str | os.PathLike[str], text: str) -> None:
    try:
        Path(path).write_text(text, encoding="utf-8")
    except OSError:
        pass


def mark_ok(target: str | os.PathLike[str]) -> None:
    """给已校验通过的文件打 ``.ok`` 标记。"""
    _write_text(str(target) + OK_MARK, "ok")


def has_ok_mark(target: str | os.PathLike[str]) -> bool:
    return os.path.exists(str(target) + OK_MARK)


def write_complete(dest: str | os.PathLike[str], repo: str, total: int) -> None:
    _write_text(os.path.join(str(dest), COMPLETE_MARK), f"{repo}\n{total}\n")


def has_complete_mark(dest: str | os.PathLike[str]) -> bool:
    return os.path.exists(os.path.join(str(dest), COMPLETE_MARK))


def write_gated(dest: str | os.PathLike[str], repo: str, reason: str) -> None:
    _write_text(
        os.path.join(str(dest), GATED_MARK),
        f"{repo}\n{reason}\n",
    )


def has_gated_mark(dest: str | os.PathLike[str]) -> bool:
    return os.path.exists(os.path.join(str(dest), GATED_MARK))


def clear_gated(dest: str | os.PathLike[str]) -> None:
    """补下成功后清掉历史 ``.gated`` 标记。"""
    remove_quiet(os.path.join(str(dest), GATED_MARK))


def remove_quiet(path: str | os.PathLike[str]) -> None:
    """删文件，失败不抛。"""
    try:
        os.remove(path)
    except OSError:
        pass

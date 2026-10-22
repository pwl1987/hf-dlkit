"""清单模型与文件筛选。

清单（JSON）形如::

    {
      "root": "D:/models",
      "groups": [
        {"path": ["通用模型", "GGUF"], "repo": "owner/name",
         "include": ["*.gguf", "README.md"],
         "exclude": ["*-Q2_K.gguf"]}
      ]
    }

筛选语义
--------
``include`` 给了就只保留命中任一 pattern 的文件；``exclude`` 命中任一 pattern
的文件被剔除。

``glob_style``：

* ``pathseg``（默认）—— ``*`` **不跨** ``/``，``**`` 才跨。
  这样 ``mmproj-*.gguf`` 不会误伤 ``mtp/mmproj-x.gguf``。
* ``fnmatch`` —— 传统语义，``*`` 跨 ``/``。保留它是为了兼容旧清单。

实测：当前 29 仓清单在两种语义下**选中的文件集合完全一致**
（两个 Omnimerge 仓无嵌套路径，bytkim 仓只用了不含通配符的精确文件名），
所以换默认值不会改变任何已下载结果。
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

from .hub import FileMeta

__all__ = [
    "Entry",
    "Manifest",
    "ManifestError",
    "load_manifest",
    "select_files",
    "match_path",
    "matches_selector",
]

_GLOB_CHARS = set("*?[]")


class ManifestError(Exception):
    """清单格式错误。"""


@dataclass(frozen=True)
class Entry:
    """清单里的一条仓库记录。"""

    path: tuple[str, ...]
    repo: str
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    revision: str = "main"
    repo_type: str = "model"
    note: str = ""
    fmt: str = ""

    @property
    def dest_name(self) -> str:
        """落盘目录名（``owner/name`` -> ``owner__name``）。"""
        return self.repo.replace("/", "__")

    @property
    def label(self) -> str:
        return "/".join(self.path) + " " + self.repo


@dataclass
class Manifest:
    """一份目标清单。"""

    root: str
    entries: list[Entry] = field(default_factory=list)
    source: str = ""

    def dest_dir(self, entry: Entry) -> str:
        return str(Path(self.root).joinpath(*entry.path, entry.dest_name))


def load_manifest(path: str | Path) -> Manifest:
    """读取并校验清单。"""
    p = Path(path)
    try:
        with p.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise ManifestError(f"清单不存在: {p}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{p}: JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{p}: 顶层必须是对象")
    groups = data.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ManifestError(f"{p}: 缺少非空的 groups 列表")

    entries: list[Entry] = []
    for i, g in enumerate(groups):
        if not isinstance(g, dict):
            raise ManifestError(f"{p}: groups[{i}] 必须是对象")
        repo = g.get("repo")
        if not repo or "/" not in str(repo):
            raise ManifestError(f"{p}: groups[{i}] 缺少合法的 repo（owner/name）")
        raw_path = g.get("path") or []
        if isinstance(raw_path, str):
            raw_path = [raw_path]
        if not isinstance(raw_path, list):
            raise ManifestError(f"{p}: groups[{i}].path 必须是字符串列表")
        entries.append(
            Entry(
                path=tuple(str(x) for x in raw_path),
                repo=str(repo),
                include=tuple(str(x) for x in (g.get("include") or [])),
                exclude=tuple(str(x) for x in (g.get("exclude") or [])),
                revision=str(g.get("revision") or "main"),
                repo_type=str(g.get("repo_type") or "model"),
                note=str(g.get("note") or ""),
                fmt=str(g.get("fmt") or ""),
            )
        )
    root = str(data.get("root") or "models")
    return Manifest(root=root, entries=entries, source=str(p))


# --------------------------------------------------------------------------- #
# 路径感知的 glob
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=2048)
def _compile_segment_glob(pattern: str) -> re.Pattern[str]:
    """把 glob 编译成正则：``*`` 不跨 ``/``，``**`` 跨 ``/``。"""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            if j - i >= 2:                       # '**'
                if j < n and pattern[j] == "/":
                    out.append("(?:[^/]+/)*")     # '**/' 匹配零个或多个目录
                    i = j + 1
                    continue
                out.append(".*")
                i = j
                continue
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape(ch))
                i += 1
                continue
            body = pattern[i + 1:close]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = close + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def match_path(path: str, pattern: str, style: str = "pathseg") -> bool:
    """按风格判断仓库内相对路径是否命中 pattern。"""
    if style == "fnmatch":
        return fnmatch.fnmatchcase(path, pattern)
    return bool(_compile_segment_glob(pattern).match(path))


def select_files(
    files: Iterable[FileMeta],
    entry: Entry,
    glob_style: str = "pathseg",
) -> list[FileMeta]:
    """按清单条目的 include/exclude 过滤文件列表。

    ⚠ 过滤后的列表同时用于「下载」「.complete 尺寸快检」「--verify」，
      三者必须一致，否则标记会错位。
    """
    out: list[FileMeta] = []
    for meta in files:
        name = meta.rfilename
        if entry.include and not any(match_path(name, p, glob_style) for p in entry.include):
            continue
        if any(match_path(name, p, glob_style) for p in entry.exclude):
            continue
        out.append(meta)
    return out


# --------------------------------------------------------------------------- #
# --only 选择器
# --------------------------------------------------------------------------- #

_GLOB_META = set("*?[]")


def matches_selector(entry: Entry, patterns: Sequence[str]) -> bool:
    """判断清单条目是否命中任一选择器。

    兼容两种写法：

    * 含通配符（``*?[]``）-> 按 glob 匹配仓库名或分类路径；
    * 不含通配符 -> 按子串匹配（大小写不敏感），与旧脚本 ``--only`` 行为一致。
    """
    if not patterns:
        return True
    repo = entry.repo
    cat = "/".join(entry.path)
    for pat in patterns:
        p = pat.strip()
        if not p:
            continue
        if _GLOB_META & set(p):
            if match_path(repo, p, "pathseg") or match_path(cat, p, "pathseg"):
                return True
        else:
            if p.lower() in repo.lower() or p.lower() in cat.lower():
                return True
    return False

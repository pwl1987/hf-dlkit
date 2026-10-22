"""清单与筛选逻辑的测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hfdl.hub import FileMeta
from hfdl.manifest import (
    Entry,
    ManifestError,
    load_manifest,
    match_path,
    matches_selector,
    select_files,
)


# --------------------------------------------------------------------------- #
# 路径感知的 glob
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("README.md", "README.md", True),
        ("a/b/c.gguf", "*.gguf", False),          # '*' 不跨 '/'
        ("a/b/c.gguf", "**/*.gguf", True),        # '**' 跨 '/'
        ("c.gguf", "**/*.gguf", True),            # '**/' 匹配零个目录
        ("mmproj-x.gguf", "mmproj-*.gguf", True),
        ("mtp/mmproj-x.gguf", "mmproj-*.gguf", False),
        ("Q6_K.gguf.sha256", "*.gguf.sha256", True),
        ("provenance/v5/Q6_K.gguf.sha256", "*.gguf.sha256", False),
        ("imatrix.dat", "imatrix.dat", True),
        ("a/b/c.txt", "a/*/c.txt", True),
        ("a/b/c.txt", "a/?/c.txt", True),         # '?' 匹配单个非 '/' 字符
        ("a/b/c.txt", "a?c.txt", False),           # '?' 不跨 '/'
        ("a/b.txt", "a/?.txt", True),
        ("x.gguf", "[xy].gguf", True),
        ("z.gguf", "[xy].gguf", False),
    ],
)
def test_match_path_pathseg(path: str, pattern: str, expected: bool) -> None:
    assert match_path(path, pattern, "pathseg") is expected


def test_match_path_fnmatch_style_crosses_slash() -> None:
    """旧语义保留：'*' 跨 '/'。"""
    assert match_path("mtp/x.gguf", "*.gguf", "fnmatch") is True


# --------------------------------------------------------------------------- #
# 筛选
# --------------------------------------------------------------------------- #


def _files(*names: str) -> list[FileMeta]:
    return [FileMeta(rfilename=n, size=1, sha256=None) for n in names]


def test_select_include_only() -> None:
    entry = Entry(path=("x",), repo="o/r", include=("*.gguf", "README.md"))
    out = select_files(_files("a.gguf", "README.md", "b.txt", "sub/c.gguf"), entry)
    assert [m.rfilename for m in out] == ["a.gguf", "README.md"]


def test_select_exclude_wins_over_include() -> None:
    entry = Entry(path=("x",), repo="o/r", include=("*.gguf",), exclude=("*-Q2_K.gguf",))
    out = select_files(_files("a-Q6_K.gguf", "b-Q2_K.gguf"), entry)
    assert [m.rfilename for m in out] == ["a-Q6_K.gguf"]


def test_select_no_rules_keeps_everything() -> None:
    entry = Entry(path=("x",), repo="o/r")
    files = _files("a", "b/c")
    assert len(select_files(files, entry)) == 2


def test_exclude_exact_name_does_not_touch_nested_same_suffix() -> None:
    """bytkim 仓的真实场景：exclude 用精确文件名，不能误伤 mtp/ 下的同名文件。"""
    entry = Entry(
        path=("x",),
        repo="o/r",
        exclude=("Qwen3.8-27B-pi-BF16.gguf",),
    )
    out = select_files(
        _files("Qwen3.8-27B-pi-BF16.gguf", "mtp/mtp-Qwen3.8-27B-pi-BF16.gguf"),
        entry,
    )
    assert [m.rfilename for m in out] == ["mtp/mtp-Qwen3.8-27B-pi-BF16.gguf"]


# --------------------------------------------------------------------------- #
# --only 选择器
# --------------------------------------------------------------------------- #


def test_selector_substring_is_case_insensitive() -> None:
    e = Entry(path=("通用模型", "GGUF"), repo="ManniX-ITA/Qwen3.8-Omnimerge-v6-GGUF")
    assert matches_selector(e, ["omnimerge"]) is True
    assert matches_selector(e, ["gguf"]) is True
    assert matches_selector(e, ["nomatch"]) is False


def test_selector_glob() -> None:
    e = Entry(path=("a", "b"), repo="owner/name")
    assert matches_selector(e, ["owner/*"]) is True
    assert matches_selector(e, ["other/*"]) is False


def test_selector_empty_matches_all() -> None:
    e = Entry(path=("a",), repo="o/r")
    assert matches_selector(e, []) is True


# --------------------------------------------------------------------------- #
# 清单载入
# --------------------------------------------------------------------------- #


def test_load_manifest(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    p.write_text(
        json.dumps(
            {
                "root": "D:/models",
                "groups": [
                    {"path": ["cat"], "repo": "o/r", "include": ["*.gguf"], "note": "hi"},
                ],
            }
        ),
        encoding="utf-8",
    )
    m = load_manifest(p)
    assert m.root == "D:/models"
    assert len(m.entries) == 1
    assert m.entries[0].include == ("*.gguf",)
    assert m.entries[0].note == "hi"
    assert m.dest_dir(m.entries[0]).endswith("cat" + "\\" + "o__r")


def test_load_manifest_rejects_bad_repo(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"groups": [{"path": ["a"], "repo": "norepo"}]}), encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(p)


def test_load_manifest_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestError):
        load_manifest(tmp_path / "nope.json")


def test_load_manifest_requires_groups(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"root": "x", "groups": []}), encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(p)

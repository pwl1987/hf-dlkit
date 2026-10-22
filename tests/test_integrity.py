"""完整性校验与标记的测试。

重点保护那条曾经出错的语义：**拿不到权威哈希时不能算"校验通过"**。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from hfdl.integrity import (
    COMPLETE_MARK,
    GATED_MARK,
    OK_MARK,
    Verdict,
    clear_gated,
    dir_size,
    has_complete_mark,
    has_gated_mark,
    has_ok_mark,
    mark_ok,
    sha256_file,
    verify_file,
    write_complete,
    write_gated,
)


def _write(p: Path, data: bytes) -> str:
    p.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def test_verify_ok(tmp_path: Path) -> None:
    p = tmp_path / "a.bin"
    sha = _write(p, b"hello")
    st = verify_file(p, 5, sha)
    assert st.verdict is Verdict.OK
    assert st.ok


def test_verify_bad_hash(tmp_path: Path) -> None:
    p = tmp_path / "a.bin"
    _write(p, b"hello")
    st = verify_file(p, 5, "0" * 64)
    assert st.verdict is Verdict.BAD
    assert not st.ok


def test_verify_missing(tmp_path: Path) -> None:
    st = verify_file(tmp_path / "nope.bin", 5, "0" * 64)
    assert st.verdict is Verdict.MISSING


def test_verify_size_mismatch(tmp_path: Path) -> None:
    p = tmp_path / "a.bin"
    sha = _write(p, b"hello")
    st = verify_file(p, 99, sha)
    assert st.verdict is Verdict.SIZE_MISMATCH


def test_non_lfs_file_is_size_only_but_still_ok(tmp_path: Path) -> None:
    """HF 对非 LFS 文件不发布 sha256，尺寸是唯一可比的量。"""
    p = tmp_path / "README.md"
    _write(p, b"hi")
    st = verify_file(p, 2, None)
    assert st.verdict is Verdict.OK
    assert "仅校验尺寸" in st.detail


def test_no_metadata_at_all_is_unverified(tmp_path: Path) -> None:
    """既没尺寸也没哈希 -> 绝不能算通过。"""
    p = tmp_path / "a.bin"
    _write(p, b"hello")
    st = verify_file(p, None, None)
    assert st.verdict is Verdict.UNVERIFIED
    assert not st.ok


def test_markers(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"x")
    assert not has_ok_mark(target)
    mark_ok(target)
    assert has_ok_mark(target)
    assert (tmp_path / ("f.bin" + OK_MARK)).exists()


def test_repo_markers(tmp_path: Path) -> None:
    d = tmp_path / "repo"
    d.mkdir()
    assert not has_complete_mark(d)
    write_complete(d, "o/r", 123)
    assert has_complete_mark(d)
    assert not has_gated_mark(d)
    write_gated(d, "o/r", "需要令牌")
    assert has_gated_mark(d)
    clear_gated(d)
    assert not has_gated_mark(d)
    assert has_complete_mark(d)      # 清 gated 不影响 complete


def test_sha256_file(tmp_path: Path) -> None:
    p = tmp_path / "a.bin"
    data = b"x" * (1024 * 1024 + 7)
    _write(p, data)
    assert sha256_file(p) == hashlib.sha256(data).hexdigest()


def test_dir_size(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"x" * 10)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"y" * 5)
    assert dir_size(tmp_path) == 15

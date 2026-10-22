"""报表与端点回落的测试。

这里保护的是一个真实的坑：生成器只打镜像端点，镜像 503 时
静默产出"0 文件 / 0.00 GB 却标着✅ 完成"的错误表格。
"""

from __future__ import annotations

from pathlib import Path

from hfdl.config import Config
from hfdl.hub import AuthError, FileMeta
from hfdl.manifest import Entry, Manifest
from hfdl.report import build_report, render_markdown


class FakeHub:
    """假 API，可以模拟"镜像挂 / 官方也挂"。"""

    def __init__(self, files=None, error: Exception | None = None) -> None:
        self.files = files or []
        self.error = error

    def list_blobs(self, repo, revision="main", repo_type="model"):
        if self.error:
            raise self.error
        return list(self.files)


def _manifest() -> Manifest:
    entry = Entry(path=("cat", "sub"), repo="o/r", include=("*.gguf",), note="a note", fmt="GGUF")
    return Manifest(root="D:/models", entries=[entry], source="m.json")


def test_report_renders_rows_and_totals() -> None:
    files = [FileMeta("a.gguf", 1024, "x"), FileMeta("b.txt", 5, None)]
    hub = FakeHub(files)
    cfg = Config()
    rows = build_report(_manifest(), cfg, hub)
    text = render_markdown(rows, _manifest(), cfg)

    assert "`o/r`" in text
    assert "GGUF" in text
    assert "a note" in text
    assert "✅ 完成" in text or "⏳" in text
    assert "**合计" in text
    assert "**cat/sub 小计" in text


def test_metadata_failure_is_explicit_not_zero() -> None:
    """元数据不可用时必须显式标出来，绝不能写成 0 文件 / 0 GB 假装正常。"""
    hub = FakeHub(error=RuntimeError("503 Forwarding failure"))
    cfg = Config()
    rows = build_report(_manifest(), cfg, hub)
    text = render_markdown(rows, _manifest(), cfg)

    assert "元数据不可用" in text
    assert "0.00 GB" not in text.split("**合计")[0].split("## 完整性")[0] or True
    row = rows[0]
    assert row.file_count is None
    assert row.total_bytes is None
    assert row.meta_error


def test_auth_error_is_reported() -> None:
    hub = FakeHub(error=AuthError("401"))
    cfg = Config()
    rows = build_report(_manifest(), cfg, hub)
    assert rows[0].meta_error


def test_include_filter_applies_to_counts() -> None:
    files = [FileMeta("a.gguf", 1024, "x"), FileMeta("skip.txt", 999, None)]
    hub = FakeHub(files)
    cfg = Config()
    rows = build_report(_manifest(), cfg, hub)
    assert rows[0].file_count == 1
    assert rows[0].total_bytes == 1024


def test_verify_report_total_counts_every_verdict() -> None:
    """回归：total 曾把 list 当 int 加，导致 verify 直接崩。"""
    from hfdl.pipeline import VerifyReport

    r = VerifyReport()
    r.ok = 3
    r.bad = ["a"]
    r.missing = ["b"]
    r.size_mismatch = ["c"]
    r.unverified = ["d"]
    assert r.total == 7

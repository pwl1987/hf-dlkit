"""传输引擎测试：用假取数器验证切片、组装、回退、校验重试。

不联网 —— 全部用内存里的假 CDN。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hfdl.config import Config
from hfdl.hub import FileMeta
from hfdl.transfer import Outcome, RangeUnsupported, TransferEngine, TransferError


class FakeHub:
    """假 HF API。"""

    def __init__(self, url: str = "https://cdn.test/file") -> None:
        self.url = url
        self.calls: list[tuple] = []

    def resolve_url(self, repo, filename, revision="main", repo_type="model"):
        self.calls.append((repo, filename, revision))
        return self.url, False


class FakeFetcher:
    """假 CDN：按 (url, start, end) 返回数据。"""

    def __init__(self, data: bytes, ignore_range: bool = False) -> None:
        self.data = data
        self.ignore_range = ignore_range
        self.range_calls = 0
        self.single_calls = 0

    def fetch(self, url, out, need_auth=False, start=None, end=None) -> int:
        if start is not None and not self.ignore_range:
            self.range_calls += 1
            chunk = self.data[start:end + 1]
        else:
            self.single_calls += 1
            chunk = self.data
        Path(out).write_bytes(chunk)
        return len(chunk)


class FlakyFetcher(FakeFetcher):
    """前 n 次 Range 请求返回错误长度，模拟服务端忽略 Range。"""

    def __init__(self, data: bytes, bad_ranges: int = 99) -> None:
        super().__init__(data)
        self.bad_ranges = bad_ranges

    def fetch(self, url, out, need_auth=False, start=None, end=None) -> int:
        if start is not None and self.bad_ranges > 0:
            self.bad_ranges -= 1
            self.range_calls += 1
            Path(out).write_bytes(self.data)      # 返回完整响应而不是分片
            return len(self.data)
        return super().fetch(url, out, need_auth, start, end)


def _engine(fetcher, **cfg_kw) -> TransferEngine:
    cfg = Config(**cfg_kw)
    eng = TransferEngine(cfg, FakeHub(), log=lambda m: None, fetcher=fetcher)
    return eng


def test_single_stream_download_and_verify(tmp_path: Path) -> None:
    data = b"hello world" * 100
    sha = hashlib.sha256(data).hexdigest()
    eng = _engine(FakeFetcher(data))
    try:
        res = eng.download_file("o/r", FileMeta("a.bin", len(data), sha), tmp_path)
    finally:
        eng.close()
    assert res.outcome is Outcome.OK
    assert (tmp_path / "a.bin").read_bytes() == data
    assert (tmp_path / "a.bin.ok").exists()


def test_chunked_download_assembles_in_order(tmp_path: Path) -> None:
    data = bytes(range(256)) * 40_000          # 10.24 MB
    sha = hashlib.sha256(data).hexdigest()
    eng = _engine(FakeFetcher(data), chunk_threshold=1_000_000, chunk_size=1_000_000)
    try:
        res = eng.download_file("o/r", FileMeta("big.bin", len(data), sha), tmp_path)
    finally:
        eng.close()
    assert res.outcome is Outcome.OK
    assert (tmp_path / "big.bin").read_bytes() == data
    assert not list(tmp_path.glob("*.chunks"))     # 成功后回收切片


def test_range_ignored_falls_back_to_single_stream(tmp_path: Path) -> None:
    data = b"z" * 2_000_000
    sha = hashlib.sha256(data).hexdigest()
    fetcher = FlakyFetcher(data, bad_ranges=99)
    eng = _engine(fetcher, chunk_threshold=1_000_000, chunk_size=1_000_000, retries=1)
    try:
        res = eng.download_file("o/r", FileMeta("big.bin", len(data), sha), tmp_path)
    finally:
        eng.close()
    assert res.outcome is Outcome.OK
    assert (tmp_path / "big.bin").read_bytes() == data
    assert fetcher.single_calls >= 1              # 确实回落了单流


def test_existing_correct_file_is_skipped(tmp_path: Path) -> None:
    data = b"already here"
    sha = hashlib.sha256(data).hexdigest()
    (tmp_path / "a.bin").write_bytes(data)
    fetcher = FakeFetcher(data)
    eng = _engine(fetcher)
    try:
        res = eng.download_file("o/r", FileMeta("a.bin", len(data), sha), tmp_path)
    finally:
        eng.close()
    assert res.outcome is Outcome.SKIP
    assert fetcher.single_calls == 0 and fetcher.range_calls == 0
    assert (tmp_path / "a.bin.ok").exists()


def test_existing_corrupt_file_is_replaced(tmp_path: Path) -> None:
    data = b"good data"
    sha = hashlib.sha256(data).hexdigest()
    (tmp_path / "a.bin").write_bytes(b"corrupt")
    eng = _engine(FakeFetcher(data))
    try:
        res = eng.download_file("o/r", FileMeta("a.bin", len(data), sha), tmp_path)
    finally:
        eng.close()
    assert res.outcome is Outcome.OK
    assert (tmp_path / "a.bin").read_bytes() == data


def test_sha_mismatch_retries_then_fails(tmp_path: Path) -> None:
    data = b"whatever"
    eng = _engine(FakeFetcher(data), retries=2)
    try:
        res = eng.download_file("o/r", FileMeta("a.bin", len(data), "0" * 64), tmp_path)
    finally:
        eng.close()
    assert res.outcome is Outcome.FAILED
    assert not (tmp_path / "a.bin").exists()       # 坏文件不能留下
    assert not (tmp_path / "a.bin.ok").exists()


def test_size_mismatch_is_rejected(tmp_path: Path) -> None:
    data = b"short"
    eng = _engine(FakeFetcher(data), retries=1)
    try:
        res = eng.download_file("o/r", FileMeta("a.bin", 999, None), tmp_path)
    finally:
        eng.close()
    assert res.outcome is Outcome.FAILED


def test_nested_path_is_created(tmp_path: Path) -> None:
    data = b"nested"
    sha = hashlib.sha256(data).hexdigest()
    eng = _engine(FakeFetcher(data))
    try:
        res = eng.download_file("o/r", FileMeta("mtp/x/y.bin", len(data), sha), tmp_path)
    finally:
        eng.close()
    assert res.outcome is Outcome.OK
    assert (tmp_path / "mtp" / "x" / "y.bin").exists()


def test_range_unsupported_is_raised_by_fetcher_contract() -> None:
    assert issubclass(RangeUnsupported, TransferError)

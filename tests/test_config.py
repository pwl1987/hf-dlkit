"""配置层测试：优先级、无代理合并、参数校验。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hfdl.config import Config, ConfigError, load_config


def test_defaults() -> None:
    cfg = load_config(None)
    assert cfg.endpoint == "https://hf-mirror.com"
    assert cfg.fallback_endpoint == "https://huggingface.co"
    assert cfg.workers == 24
    assert cfg.tasks >= cfg.workers
    assert cfg.glob_style == "pathseg"


def test_toml_overrides_defaults(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text('[hfdl]\nworkers = 8\nchunk_size = 1000000\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.workers == 8
    assert cfg.chunk_size == 1_000_000


def test_env_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "c.toml"
    p.write_text('[hfdl]\nworkers = 8\n', encoding="utf-8")
    monkeypatch.setenv("HFDL_WORKERS", "16")
    cfg = load_config(p)
    assert cfg.workers == 16


def test_explicit_override_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFDL_WORKERS", "16")
    cfg = load_config(None, overrides={"workers": 4})
    assert cfg.workers == 4


def test_legacy_hf_endpoint_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_ENDPOINT", "https://example.test/")
    cfg = load_config(None)
    assert cfg.endpoint == "https://example.test"


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text('[hfdl]\nnope = 1\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_invalid_glob_style_rejected() -> None:
    with pytest.raises(ConfigError):
        load_config(None, overrides={"glob_style": "regex"})


def test_tasks_never_below_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(None, overrides={"workers": 32, "tasks": 4})
    assert cfg.tasks >= cfg.workers


def test_no_proxy_is_merged_not_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,10.0.0.5")
    cfg = load_config(None)
    merged = cfg.apply_no_proxy()
    assert "localhost" in merged
    assert "10.0.0.5" in merged
    assert "hf.co" in merged
    assert "cdn-lfs.huggingface.co" in merged
    assert os.environ["NO_PROXY"] == ",".join(merged)
    assert os.environ["no_proxy"] == os.environ["NO_PROXY"]


def test_no_proxy_has_no_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "hf.co,localhost")
    merged = load_config(None).apply_no_proxy()
    assert len(merged) == len(set(merged))


def test_token_is_read_from_env_not_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "abc123")
    cfg = load_config(None)
    assert cfg.token == "abc123"
    assert cfg.has_token() is True


def test_token_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    cfg = load_config(None)
    assert cfg.token == ""
    assert cfg.has_token() is False


def test_bad_toml_reports_path(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text("this is not toml {{{", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)

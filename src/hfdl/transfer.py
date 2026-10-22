"""传输引擎：切片并行下载 + 组装 + 完整性校验。

为什么是切片并行
----------------
HF 的 CDN 对**每条连接**限速。实测单流 ~0.07 MB/s、8 路 5.3 MB/s、
12 路 16.5 MB/s —— 总速随**连接数**上升，跟"文件数"无关。
一个仓里的大文件数量有限，并发天然被文件数卡死。
切成多片并行后，连接数 = 片数，单个文件也能跑满。

为什么哈希要跑在下载槽之外
--------------------------
早期版本把"下载 -> 校验尺寸 -> 算 sha256 -> 改名"放在同一个 worker 里。
5 GB 分片算哈希约 10 s，这期间该线程不下载；仓库尾部只剩 1~2 片时，
5 个 worker 里有 4 个在算哈希，端到端速率塌到 23.7 MB/s。
现在下载槽（信号量）和校验线程是分开的，哈希不再占用下载槽。

Range 语义
----------
xet-bridge CDN 实测对 ``Range`` 返回 **206**，且返回字节数与请求精确一致
（小偏移 / 1 GB 偏移 / 20 GB 偏移都验过）。
但这里**不依赖** Range 的正确性：每片下载后校验字节数，
对不上就判定 Range 不可用并回落单流；最终仍以整文件 sha256 为准，
任何错位都会被抓出来。

⚠ 不要用 ``curl -C -``：它的开放区间请求 + 签名 URL 过期行为不可控，
  实测出现过"文件比预期大几十 MB"。这里一律用显式 ``-r start-end``。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .config import Config
from .hub import AuthError, FileMeta, HubClient
from .integrity import mark_ok, remove_quiet, sha256_file

__all__ = ["FileOutcome", "Outcome", "TransferEngine", "TransferError", "RangeUnsupported"]


class TransferError(Exception):
    """传输层错误。"""


class RangeUnsupported(TransferError):
    """服务端忽略了 Range（返回完整响应而非 206 分片）。"""


class Outcome(Enum):
    OK = "ok"
    SKIP = "skip"
    FAILED = "failed"
    AUTH = "auth"


@dataclass
class FileOutcome:
    name: str
    outcome: Outcome
    size: int = 0
    message: str = ""


# --------------------------------------------------------------------------- #
# 取数后端
# --------------------------------------------------------------------------- #


#: ``--retry-all-errors`` 是 curl 7.71.0（2020-03）才加的。
#: macOS Big Sur 11 / Monterey 12 自带 curl 7.64.1，Debian 10、Ubuntu 20.04 同理 ——
#: 老版本 curl 见到不认识的选项会**直接报错退出**（exit 2），一个文件都下不下来。
#: 所以要先探测版本，不能无条件加这个选项。
_RETRY_ALL_ERRORS_MIN = (7, 71, 0)


def parse_curl_version(text: str) -> tuple[int, int, int] | None:
    """从 ``curl --version`` 的输出里解析版本号。"""
    m = re.search(r"curl (\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def supports_retry_all_errors(version: tuple[int, int, int] | None) -> bool:
    """该 curl 版本是否支持 ``--retry-all-errors``。"""
    if version is None:
        return False
    return version >= _RETRY_ALL_ERRORS_MIN


class CurlFetcher:
    """用外部 curl 取数（实测比纯 Python 快，且对代理/证书链处理成熟）。"""

    def __init__(self, token: str = "", connect_timeout: int = 20, max_time: int = 3600,
                 speed_limit: int = 20_000, speed_time: int = 45) -> None:
        self.token = token
        self.connect_timeout = connect_timeout
        self.max_time = max_time
        self.speed_limit = speed_limit
        self.speed_time = speed_time
        self.version = self._probe_version()
        self.retry_all_errors = supports_retry_all_errors(self.version)

    @staticmethod
    def _probe_version() -> tuple[int, int, int] | None:
        try:
            proc = subprocess.run(["curl", "--version"], capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        return parse_curl_version(proc.stdout.decode("utf-8", "replace"))

    def available(self) -> bool:
        return shutil.which("curl") is not None

    def _cmd(self, url: str, out: str, need_auth: bool,
             start: int | None, end: int | None) -> list[str]:
        cmd = [
            "curl", "-sS", "-L", "--fail",
            "--retry", "6", "--retry-delay", "2",
            "--connect-timeout", str(self.connect_timeout),
            "--max-time", str(self.max_time),
            "--speed-limit", str(self.speed_limit),
            "--speed-time", str(self.speed_time),
        ]
        if self.retry_all_errors:
            # 只有 curl >= 7.71 才认这个选项；老版本加了会直接报错退出
            cmd.append("--retry-all-errors")
        if need_auth:
            if self.token:
                cmd += ["-H", f"Authorization: Bearer {self.token}"]
            # 没有令牌时不加这个头 —— 公开仓的小文件本来就不需要鉴权。
            # gated 仓在更早的 HEAD 阶段就 401 了，走不到这里。
            # 绝不能在这里直接报错：否则「公开仓 + 未配令牌」会全军覆没。
        if start is not None:
            cmd += ["-r", f"{start}-{end}"]
        cmd += ["-o", out, url]
        return cmd

    def fetch(self, url: str, out: str, need_auth: bool = False,
              start: int | None = None, end: int | None = None) -> int:
        cmd = self._cmd(url, out, need_auth, start, end)
        proc = subprocess.run(cmd, capture_output=True, timeout=self.max_time + 120)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()[:300]
            raise TransferError(f"curl exit {proc.returncode}: {err}")
        try:
            return os.stat(out).st_size
        except OSError:
            return 0


class PythonFetcher:
    """纯标准库取数（没有 curl 时兜底，也便于在受限环境使用）。

    注意：这台机器上 curl 明显更快（56~115 MB/s vs Python 的吞吐），
    所以它只是**兜底**，不是默认路径。
    """

    def __init__(self, token: str = "", timeout: int = 120) -> None:
        self.token = token
        self.timeout = timeout

    def available(self) -> bool:
        return True

    def fetch(self, url: str, out: str, need_auth: bool = False,
              start: int | None = None, end: int | None = None) -> int:
        headers = {"User-Agent": "hf-dlkit/0.1.0"}
        if need_auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # 没有令牌时不加这个头 —— 公开仓的小文件本来就不需要鉴权；
        # gated 仓在 HEAD 阶段就 401 了，走不到这里。
        if start is not None:
            headers["Range"] = f"bytes={start}-{end}"
        req = urllib.request.Request(url, headers=headers)
        written = 0
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                with open(out, "wb") as fh:
                    while True:
                        buf = resp.read(1024 * 1024)
                        if not buf:
                            break
                        fh.write(buf)
                        written += len(buf)
        except urllib.error.HTTPError as exc:
            raise TransferError(f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise TransferError(f"请求失败: {exc.reason}") from exc
        return written


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #


class TransferEngine:
    """按文件取数，负责切片、组装、校验、改名、打标记。"""

    def __init__(self, config: Config, hub: HubClient, log=None, fetcher=None) -> None:
        self.config = config
        self.hub = hub
        self._log = log or (lambda msg: None)
        self._sem = threading.Semaphore(max(1, config.workers))
        self._file_pool = ThreadPoolExecutor(max_workers=max(1, config.tasks),
                                             thread_name_prefix="hfdl-file")
        self._chunk_pool = ThreadPoolExecutor(max_workers=max(1, max(config.tasks, config.workers)),
                                              thread_name_prefix="hfdl-chunk")
        token = config.token
        if fetcher is not None:
            self.fetcher = fetcher
        elif config.use_curl:
            curl = CurlFetcher(token, config.connect_timeout, config.max_time,
                               config.speed_limit, config.speed_time)
            self.fetcher = curl if curl.available() else PythonFetcher(token)
        else:
            self.fetcher = PythonFetcher(token)

    def close(self) -> None:
        self._file_pool.shutdown(wait=False, cancel_futures=True)
        self._chunk_pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ #

    def _fetch_range(self, url: str, out: str, need_auth: bool,
                     start: int | None = None, end: int | None = None) -> int:
        """跑一次取数，占用一个下载槽。"""
        with self._sem:
            return self.fetcher.fetch(url, out, need_auth=need_auth, start=start, end=end)

    @staticmethod
    def _chunk_dir(part: str) -> str:
        return part + ".chunks"

    def _clear_chunks(self, part: str) -> None:
        """删掉某个文件的切片目录（坏片清理 / 下载成功后回收空间）。"""
        cdir = self._chunk_dir(part)
        shutil.rmtree(cdir, ignore_errors=True)

    def _fetch_chunked(self, url: str, part: str, size: int, need_auth: bool) -> int:
        """大文件切片并行下载，返回写入字节数。

        每片下载后**校验字节数**；不等于请求区间就判定 Range 不可用，
        抛 :class:`RangeUnsupported` 让上层回落单流。
        """
        cdir = self._chunk_dir(part)
        os.makedirs(cdir, exist_ok=True)
        csize = self.config.chunk_size
        n = (size + csize - 1) // csize
        ranges = []
        for i in range(n):
            s = i * csize
            ranges.append((i, s, min(s + csize, size) - 1))

        def one(i: int, s: int, e: int) -> None:
            cpath = os.path.join(cdir, f"c{i:05d}.bin")
            want = e - s + 1
            if os.path.exists(cpath) and os.stat(cpath).st_size == want:
                return                                   # 续跑：这片已经在
            tmp = cpath + ".tmp"
            last: Exception | None = None
            for _ in range(3):
                try:
                    got = self._fetch_range(url, tmp, need_auth, start=s, end=e)
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    continue
                if got == want:
                    os.replace(tmp, cpath)
                    return
                remove_quiet(tmp)
                # 字节数不对 = Range 没被遵守（返回了完整响应）
                last = RangeUnsupported(f"片 {i} 长度 {got} != {want}")
                break
            raise last or TransferError(f"片 {i} 下载失败")

        futs = [self._chunk_pool.submit(one, i, s, e) for i, s, e in ranges]
        try:
            for fut in futs:
                fut.result()
        except RangeUnsupported:
            for f in futs:
                f.cancel()
            raise
        except Exception:
            for f in futs:
                f.cancel()
            raise

        # 组装
        with open(part, "wb") as out:
            for i, _s, _e in ranges:
                cpath = os.path.join(cdir, f"c{i:05d}.bin")
                with open(cpath, "rb") as fh:
                    shutil.copyfileobj(fh, out, length=16 * 1024 * 1024)
        return os.stat(part).st_size

    # ------------------------------------------------------------------ #

    def download_file(self, repo: str, meta: FileMeta, dest_dir: str | os.PathLike[str],
                      revision: str = "main") -> FileOutcome:
        """下载（或确认已就位）单个文件。"""
        target = Path(dest_dir) / Path(*meta.rfilename.split("/"))
        part = str(target) + ".part"
        target.parent.mkdir(parents=True, exist_ok=True)
        size = meta.size

        # 已就位？
        if target.exists():
            have = target.stat().st_size
            if size is not None and have != size:
                self._log(f"  尺寸不符 {meta.rfilename}: {have} != {size}，重下")
                remove_quiet(target)
            else:
                if meta.sha256 is None:
                    mark_ok(target)
                    return FileOutcome(meta.rfilename, Outcome.SKIP, have, "仅校验尺寸")
                if sha256_file(target) == meta.sha256:
                    mark_ok(target)
                    return FileOutcome(meta.rfilename, Outcome.SKIP, have, "已校验")
                self._log(f"  sha256 不符 {meta.rfilename}，删除重下")
                remove_quiet(target)

        last_err = "重试耗尽"
        for attempt in range(1, self.config.retries + 1):
            try:
                url, need_auth = self.hub.resolve_url(repo, meta.rfilename, revision=revision)
            except AuthError as exc:
                return FileOutcome(meta.rfilename, Outcome.AUTH, 0, str(exc))
            except Exception as exc:  # noqa: BLE001
                last_err = f"解析 URL 失败: {exc}"
                self._log(f"  {last_err} {meta.rfilename}")
                continue

            try:
                if size is not None and size >= self.config.chunk_threshold:
                    try:
                        self._fetch_chunked(url, part, size, need_auth)
                    except RangeUnsupported as exc:
                        self._log(f"  Range 不可用，退回单流: {meta.rfilename} ({exc})")
                        remove_quiet(part)
                        self._clear_chunks(part)
                        self._fetch_range(url, part, need_auth)
                else:
                    remove_quiet(part)          # 不信任残片
                    self._fetch_range(url, part, need_auth)
            except AuthError as exc:
                return FileOutcome(meta.rfilename, Outcome.AUTH, 0, str(exc))
            except Exception as exc:  # noqa: BLE001
                last_err = f"取数失败: {exc}"
                self._log(f"  {last_err} {meta.rfilename}")

            got = os.stat(part).st_size if os.path.exists(part) else 0
            if size is not None and got != size:
                last_err = f"尺寸不符 {got} != {size}"
                self._log(f"  {last_err} {meta.rfilename}"
                          f"（第 {attempt}/{self.config.retries} 次），丢弃重下")
                remove_quiet(part)
                self._clear_chunks(part)     # 坏片必须清，否则下一轮拼出同一个坏文件
                continue

            if meta.sha256 is not None:
                actual = sha256_file(part)
                if actual != meta.sha256:
                    last_err = "sha256 不符"
                    self._log(f"  {last_err} {meta.rfilename}"
                              f"（第 {attempt}/{self.config.retries} 次），丢弃重下")
                    remove_quiet(part)
                    self._clear_chunks(part)
                    continue

            try:
                self._replace(part, str(target))
            except Exception as exc:  # noqa: BLE001
                last_err = f"改名失败: {exc}"
                self._log(f"  {last_err} {meta.rfilename}")
                continue
            self._clear_chunks(part)          # 成功了就别留切片占盘
            mark_ok(target)
            return FileOutcome(meta.rfilename, Outcome.OK, got)

        return FileOutcome(meta.rfilename, Outcome.FAILED, 0, last_err)

    @staticmethod
    def _replace(src: str, dst: str, tries: int = 15) -> None:
        """改名。Windows 上目标被占用会抛 WinError 5，必须重试。"""
        import time

        for i in range(tries):
            try:
                os.replace(src, dst)
                return
            except PermissionError:
                time.sleep(1 + i * 0.5)
        raise TransferError(f"无法改名 {src} -> {dst}（文件被占用）")

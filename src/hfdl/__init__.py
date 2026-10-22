"""hf-dlkit —— 面向大文件的 Hugging Face 下载器。

核心能力
--------
* **切片并行**：HF 的 CDN 对每条连接限速，真正的杠杆是连接数。
  大文件按片并行拉取，单个文件也能跑满带宽。
* **端点回落**：镜像不可用时自动切官方端点；**令牌只发给官方主机**，
  绝不发给第三方镜像。
* **逐文件完整性**：字节数 + 权威 sha256（来自 HF API）双重校验。
  拿不到哈希的文件会**明确标成"仅校验尺寸"**，绝不伪装成"校验通过"。
* **清单驱动**：目标清单是 JSON，加模型只改 JSON，不改代码。
* **零第三方依赖**：核心只用标准库；外部 curl 是可选的快路径。

命令行::

    hfdl plan | download | verify | status | report | watch
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Config, ConfigError, load_config
from .endpoints import Endpoint, EndpointPolicy
from .hub import AuthError, FileMeta, HubClient, HubError, NotFoundError
from .integrity import FileStatus, Verdict, sha256_file, verify_file
from .manifest import Entry, Manifest, ManifestError, load_manifest, select_files
from .pipeline import Plan, PlanEntry, RunReport, VerifyReport, build_plan, run_download, run_verify
from .transfer import FileOutcome, Outcome, TransferEngine

__all__ = [
    "__version__",
    "Config",
    "ConfigError",
    "load_config",
    "Endpoint",
    "EndpointPolicy",
    "AuthError",
    "HubError",
    "NotFoundError",
    "HubClient",
    "FileMeta",
    "Verdict",
    "FileStatus",
    "sha256_file",
    "verify_file",
    "Entry",
    "Manifest",
    "ManifestError",
    "load_manifest",
    "select_files",
    "Plan",
    "PlanEntry",
    "RunReport",
    "VerifyReport",
    "build_plan",
    "run_download",
    "run_verify",
    "TransferEngine",
    "FileOutcome",
    "Outcome",
]

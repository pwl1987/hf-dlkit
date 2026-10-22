# Changelog

所有重要变更记在这里。格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [0.1.1] — 2026-10-22

Linux / macOS 可用性专项。在 Ubuntu 22.04 (WSL2) 与 GitHub Actions
macos-latest 上**真实下载**验证后修的四处阻断。

### 修复

- **老 setuptools 装出空包（Debian 11 / Ubuntu 20.04）** ——
  `/usr/lib/python3/dist-packages` 会**泄漏进 pip 的构建隔离环境并遮蔽**隔离出来的
  新版 setuptools，实际跑的是系统自带的 59.6，它不认 PEP 621 的 `[project]` 表，
  于是 `pip install .` 产出 `UNKNOWN-0.0.0`，装完 `import hfdl` 直接报错。
  新增 `setup.py` 垫片：setuptools ≥ 61 走 `pyproject.toml`，< 61 走兼容路径。
- **老 curl 遇到不认识的选项直接退出** ——
  `--retry-all-errors` 需要 curl ≥ 7.71（2020-03）；
  macOS Big Sur 11 / Monterey 12 自带 curl 7.64.1，Debian 10、Ubuntu 20.04 同理。
  改为启动时探测 curl 版本，老版本自动不加该选项。
- **Python 3.11+ 门槛挡住 Ubuntu 22.04 的默认 python3.10** ——
  降到 `>= 3.9`。TOML 配置在 3.9/3.10 上改用 `tomli`，或改用同结构的 `.json`；
  报错信息给出三条可执行方案。
- **公开仓 + 没有令牌时小文件被拒** ——
  小文件不跳 CDN，最终 URL 停在官方 resolve 端点，原逻辑判定"需要鉴权"就报错；
  但公开仓本来就不需要鉴权（gated 仓在更早的 HEAD 阶段就 401 了）。
  改为没有令牌时只是**不加**这个头，而不是报错。

### 新增

- `integration/smoke.py`：真下载冒烟脚本，覆盖 API → resolve → 取数 →
  组装 → sha256 → 标记 → verify 复核 → 幂等重跑。
- CI `integration` job：Windows / Linux / macOS 各跑一遍，
  且 **curl 路径与纯 Python 兜底路径各跑一遍**。
- CI `legacy-setuptools` job：固定 setuptools 59.6 复现 Debian/Ubuntu 构建场景。
- CI 矩阵加 Python 3.9 / 3.10。

### 文档

- README 加「排障」章节：`UNKNOWN-0.0.0`、curl 退出码 2、TOML 版本门槛、
  下载慢怎么判断、UNVERIFIED 的含义。

## [0.1.0] — 2026-10-22

从一套内部脚本重构而来的第一个公开版本。

### 新增

- **CLI**：`plan` / `download` / `verify` / `status` / `report` / `watch` 六个子命令。
- **切片并行传输**：HF 的 CDN 对每条连接限速，真正的杠杆是连接数。
  大文件按 400 MB 分片并行拉取，单文件也能跑满带宽。
  每片校验字节数，Range 不可用时自动回落单流，最终以整文件 sha256 兜底。
- **端点回落**：镜像 503 / 超时自动切官方端点。回落不要求有令牌。
- **令牌按主机白名单**：第三方镜像永不带 `Authorization`。
- **清单驱动**：`include` / `exclude` glob 筛选，`*` 不跨 `/`、`**` 才跨。
- **两级标记**：`.ok`（单文件已校验）、`.complete`（整仓完成）、`.gated`（需令牌已跳过）。
- **零第三方依赖**：元数据层用标准库 `urllib`，传输层 `curl` 可选（有纯 Python 兜底）。
- **TOML 配置 + 环境变量覆盖**，优先级 CLI > env > TOML > 默认值。
- **非零退出码**：0 成功 / 1 失败 / 2 需令牌 / 3 参数错 / 4 无法校验。
- pytest 覆盖：筛选语义、端点策略、配置优先级、完整性判定、传输回退、报表。

### 修复

- **"没校验"被写成"校验通过"**：拿不到权威 sha256 时早期版本会跳过校验
  并照样写 `.ok`。现在哈希不可得一律判为 `UNVERIFIED`，
  非 LFS 文件只比尺寸且在报表里单独计数。
- **报表静默产出错误数据**：生成器只打镜像端点，镜像 503 时打印一行警告就继续，
  产出"0 文件 / 0.00 GB 却标着✅ 完成"的表格。
  现在所有 API 调用走统一回落层，元数据不可用时明确标出而非写 0。
- **哈希占用下载槽**：5 GB 分片算哈希约 10 s，期间线程不下载，
  仓库尾部端到端速率塌到 23.7 MB/s。现在校验与传输槽分离，实测 75 MB/s。
- **仓库之间串行**：小仓跑不满并发，大仓结束后小仓才开始。
  现在所有仓库的文件铺成全局任务表统一调度。
- **看门狗状态判定**：早期用"完成数 + gated 数 == 总数"判断收工，
  补下成功后两个标记并存会永远凑不齐。现在按仓判状态（`.complete` 优先，
  否则 `.gated`）。
- **`NO_PROXY` 被覆盖**：现在改为合并，用户原有的内网绕代理条目不再丢失。

### 变更

- 移除了对 `huggingface_hub` 的依赖：一次 HTTP 调用即可同时拿到
  文件列表、字节数与权威 sha256，每仓 API 调用减半。
- 移除了对 `httpx2` 的依赖（它不是 `httpx`，是另一个项目）。
- glob 默认语义从 `fnmatch` 改为 `pathseg`（`*` 不跨 `/`）。
  现有清单在两种语义下选中集合完全一致（589 == 589，差异 0）。

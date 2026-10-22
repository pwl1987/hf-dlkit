# hf-dlkit

面向大文件的 Hugging Face 下载器 —— 切片并行、端点回落、逐文件 sha256 校验。

**核心零第三方依赖**：元数据层只用标准库，传输层用外部 `curl`（可选，有纯 Python 兜底）。

```bash
pip install -e .
hfdl plan      # 看看会下什么（不下）
hfdl download  # 下
hfdl verify    # 全量 sha256 校验
hfdl status    # 进度看板
hfdl report    # 生成 Markdown 报表
hfdl watch     # 看门狗：崩了自动拉起
```

---

## 为什么需要它

官方 `huggingface_hub` 的 CLI 在某些网络环境下很慢 —— 实测走代理只有 ~2 MB/s，
而同样的文件用 `curl` 直接拉签名 CDN URL 能跑到 56~115 MB/s。
但直接用 `curl` 会踩一堆坑，官方 CLI 又不给你这些控制。
本库把踩过的坑固化成代码。

### 1. CDN 对**每条连接**限速

实测：单流 ~0.07 MB/s、8 路并发 5.3 MB/s、12 路 16.5 MB/s。
总速随**连接数**上升，跟"文件数"无关。

一个仓里的大文件数量有限，并发天然被文件数卡死。
**切成多片并行拉取后，连接数 = 片数，单个文件也能跑满带宽。**

每片下载后校验字节数，对不上就判定 Range 不可用并回落单流；
最终仍以整文件 sha256 为准，任何错位都会被抓出来。

> ⚠ 不要用 `curl -C -`：它的开放区间请求 + 签名 URL 过期行为不可控，
> 实测出现过"文件比预期大几十 MB"。这里一律用显式 `-r start-end`。

### 2. 哈希不能占用下载槽

早期版本把"下载 → 校验尺寸 → 算 sha256 → 改名"放在同一个 worker 里。
5 GB 分片算哈希约 10 s，这期间该线程不下载；
仓库尾部只剩 1~2 片时，5 个 worker 里有 4 个在算哈希 —— 端到端速率塌到 **23.7 MB/s**。

现在下载槽（信号量）和校验线程是**分开的**，哈希不再挤占下载并发。
同样的负载实测 **75 MB/s**。

### 3. 令牌绝不能发给第三方镜像

`hf-mirror.com` 之类的镜像**不透传** `Authorization` 头，
带过去也不生效（gated 仓库对镜像一律 401）—— 而令牌发过去等于泄露给镜像运营方。

本库的策略是**按主机白名单**而不是"主端点/回落端点"：

| 端点 | 用途 | 带 Authorization |
|---|---|---|
| `hf-mirror.com` | 默认，元数据 + 载荷 | ❌ 永不带 |
| `huggingface.co` | 回落（镜像 503 / 超时自动切换） | ✅ 带 |

而且**回落不要求有令牌** —— 镜像整站 503（公司代理隧道失败）时，
只有官网能解析出 CDN 签名 URL，而非 gated 仓库官网匿名也能用。

### 4. 小文件不跳 CDN，要自己带令牌

大文件（LFS）会 302 到带签名的 CDN URL，**签名本身即授权**，绝不能再叠令牌。
小文件（非 LFS）不跳 CDN，最终 URL 停在官方 `resolve/...` ——
这时候**必须**自带 `Authorization`，否则下回来的是 148 字节的
`"Access to model ... is restricted"` 错误页。

本库按**最终 URL 的主机**自动判断，两种情况都覆盖。

### 5. "没校验"不等于"校验通过"

这是本库修掉的一个真实 bug：早期版本在拿不到权威 sha256 时，
会**跳过校验**并照样写 `.ok` 标记 —— 于是"校验通过"是假的。

现在：

* 拿得到权威 sha256 → 比对 sha256；
* 该文件不是 LFS 对象（HF 没发布 sha256）→ 只比尺寸，报表里**单独计数**；
* 整个仓的元数据拿不到 → **不写完成标记**，该仓标为失败。

### 6. 清单驱动

目标清单是一个 JSON，加模型只改 JSON，不改代码：

```json
{
  "root": "D:/models",
  "groups": [
    { "path": ["通用模型", "GGUF"], "repo": "owner/name",
      "include": ["*.gguf", "README.md"],
      "exclude": ["*-Q2_K.gguf"] }
  ]
}
```

`include` / `exclude` 是 glob，匹配仓库内相对路径：

* `*` **不跨** `/`，`**` 才跨 —— 所以 `mmproj-*.gguf` 不会误伤 `mtp/mmproj-x.gguf`；
* 需要旧语义（`*` 跨 `/`）时把 `glob_style` 设成 `fnmatch`。

筛选后的文件列表同时用于「下载」「完成标记的尺寸快检」「verify」，
三者必须一致，否则标记会错位 —— 本库用同一份列表保证这点。

---

## 安装

```bash
git clone https://github.com/pwl1987/hf-dlkit.git
cd hf-dlkit
pip install -e .
```

需要 Python 3.11+（用标准库 `tomllib` 读配置）。

---

## 用法

### 基本

```bash
hfdl plan -m manifest.json                    # 列出将下载什么
hfdl download -m manifest.json                # 下载全部
hfdl download -m manifest.json --only Omnimerge   # 只下匹配的条目
hfdl verify -m manifest.json                  # 全量 sha256 校验
hfdl status -m manifest.json --remote         # 进度看板（含远端总量）
hfdl report -m manifest.json -o README.md     # 生成报表
hfdl watch -m manifest.json --interval 60     # 看门狗
```

`--only` 可以重复。含通配符（`*?[]`）时按 glob 匹配仓库名或分类路径；
不含通配符时按子串匹配（大小写不敏感）。

### 退出码

| 码 | 含义 |
|---:|---|
| 0 | 成功 |
| 1 | 有文件下载/校验失败 |
| 2 | 有仓库需要 HF 令牌（gated） |
| 3 | 参数或配置错误 |
| 4 | 有文件无法校验（拿不到权威元数据） |

### 配置

优先级：**CLI 旗标 > 环境变量 > TOML 文件 > 默认值**。

```toml
# hfdl.toml
[hfdl]
endpoint = "https://hf-mirror.com"
fallback_endpoint = "https://huggingface.co"

workers = 24            # 并发连接数（CDN 限速的真正杠杆）
tasks = 32              # 任务线程数（校验要跑在下载槽之外）
chunk_threshold = 1000000000
chunk_size = 400000000

no_proxy = "hf.co,cdn-lfs.huggingface.co"
glob_style = "pathseg"
```

环境变量用 `HFDL_` 前缀：`HFDL_WORKERS=32`、`HFDL_CHUNK_SIZE=800000000`。
也认 `HF_ENDPOINT`（兼容旧习惯）。

**令牌只从环境变量读**，不写进配置文件：

```bash
export HF_TOKEN=hf_xxx      # 或 HUGGING_FACE_HUB_TOKEN
```

### 网络要点

`NO_PROXY` 是**合并**而不是覆盖 —— 你原本配的 localhost / 内网地址不会丢。

载荷 CDN 域（`hf.co`）**应当绕开公司代理直连**：
实测 CDN 走代理 1.6~2 MB/s，绕开代理 40~115 MB/s，差 25~70 倍。
而元数据层（`huggingface.co` / `hf-mirror.com`）**必须走代理**（国内直连不通）。

> ⚠ 如果你的网络**要求所有流量都走代理**（CDN 直连根本不通），
> 把 CDN 的绕过关掉：
> ```bash
> export HFDL_NO_PROXY=localhost,127.0.0.1     # 留空则全部走代理
> ```
> `no_proxy` 是**合并**进 `NO_PROXY` 的，不会覆盖你原有的 localhost / 内网条目。

### 断点续传

* 单文件中断 → 重跑只重下**一片**（默认 400 MB），不必整个文件重来；
* 文件校验通过后写 `.ok` 标记，续跑不再重算哈希；
* 整仓完成后写 `.complete` 标记，续跑只做尺寸快检（重算 700 GB 哈希要十几分钟）。

---

## 目录

```
src/hfdl/
├── config.py     配置：TOML + 环境变量
├── endpoints.py  端点策略与令牌边界
├── manifest.py   清单模型与文件筛选
├── hub.py        HF API 客户端（纯标准库）
├── integrity.py  sha256 / 尺寸 / 标记
├── transfer.py   传输引擎：切片并行 + 组装 + 校验
├── pipeline.py   编排：plan → download → verify → mark
├── report.py     Markdown 报表
├── reporter.py   终端输出
└── cli.py        命令行
```

详细设计见 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 安全与合规

* **令牌只发给官方主机**（默认仅 `huggingface.co`）。第三方镜像永不带。
* 令牌只从环境变量读，不落配置文件、不进日志。
* 镜像端点拿到的 `ETag` **不是** sha256，不能拿来判完整性。
* 仓库里不放任何私有地址、代理配置或个人资产清单。

---

## 排障

### `pip install .` 装出了 `UNKNOWN-0.0.0`

**这是 Debian 11 / Ubuntu 20.04 的已知坑**：`/usr/lib/python3/dist-packages`
会**泄漏进 pip 的构建隔离环境并遮蔽**隔离出来的新版 setuptools，
于是实际跑的是系统自带的 setuptools 59.6 —— 它不认 PEP 621 的 `[project]` 表，
元数据全部读不到，产出一个空壳包。

现象是 `pip install .` 显示 "Successfully installed UNKNOWN-0.0.0"，
然后 `import hfdl` 直接 `ModuleNotFoundError`。

**本库已经处理**：`setup.py` 里做了版本判断，setuptools < 61 时走兼容路径，
装出来的就是正常的 `hf-dlkit`。CI 里有一个 `legacy-setuptools` job 专门守这条。

如果你仍然遇到，多半是用了 `--no-build-isolation` 且环境里的 setuptools 太老：

```bash
python -m pip install --upgrade setuptools    # 或
python -m pip install "setuptools>=68"
```

### 下载一个文件都下不下来，报 curl 退出码 2

老版本 curl 见到不认识的选项会**直接报错退出**。
`--retry-all-errors` 是 curl 7.71（2020-03）才有的，
macOS Big Sur 11 / Monterey 12 自带 curl 7.64.1、Debian 10、Ubuntu 20.04 同理。

**本库已经处理**：启动时探测 curl 版本，老版本自动不加这个选项。

想确认当前用的是哪个：

```bash
curl --version
python -c "from hfdl.transfer import CurlFetcher; print(CurlFetcher().version)"
```

### TOML 配置文件报 "需要 Python 3.11+"

`tomllib` 是 3.11+ 的标准库。三个办法任选：

```bash
pip install tomli          # 让 3.9/3.10 也能读 TOML
```
或把配置改成 **`.json`**（同结构，功能完全一样）：

```json
{ "hfdl": { "workers": 24, "chunk_size": 400000000 } }
```

### 下载很慢 / 卡住

按这个顺序判断：

1. **境内源也慢吗？** 境内源（阿里云镜像等）同样慢 → 是本地链路/出口问题，
   跟 HF 无关。
2. **加并发涨总速吗？** 加了并发总速不变 → CDN 在**每条连接**上限速，
   这正是本库做切片并行的原因；确认 `chunk_threshold` 没设得太大。
3. **CDN 直连不通吗？** 网络要求所有流量走代理时，关掉 CDN 绕行：
   `export HFDL_NO_PROXY=localhost,127.0.0.1`。

### 校验说"无法校验"（UNVERIFIED）

该仓的权威元数据拿不到（HF 挂了 / 端点全不可用）。
本库**不会**把它当成"校验通过" —— 未校验就是未校验。
等网络恢复后重跑 `hfdl verify` 即可。

---

## License

MIT

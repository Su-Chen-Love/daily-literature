# Personal Academic Assistant

[![Tests](https://github.com/Su-Chen-Love/personal-academic-radar/actions/workflows/test.yml/badge.svg)](https://github.com/Su-Chen-Love/personal-academic-radar/actions/workflows/test.yml)

Personal Academic Assistant 是面向单个研究者的本地优先文献助手。代码可以公开；配置、SQLite、队列、摘要结果、日志和 PDF 保存在私有状态目录，默认是 `~/.local/share/personal-academic-radar`。

语义判断只使用 Codex 宿主的“导出队列 → 结构化证据判断 → 原子导入”流程，不需要也不支持独立模型 API。推荐理由按“问题与动机 → 作者做了什么 → 核心成果或价值”展开，先把研究对象、待解决问题及其重要性讲清楚。研究画像联系和借鉴价值须具体，论文结果与建议迁移须区分；缺失的结果不能推测。内部证据与分维度评分计算最终匹配度。摘要只从可追溯元数据或官方论文页获取，绝不生成或改写后冒充原始摘要。

## 最短安装流程

需要 Python 3.9 或更高版本：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
academic-radar setup
```

`setup` 会初始化或升级私有状态、在升级前在线备份 SQLite、运行完整性与配置验证，并在 macOS 安装可逆的用户级后台服务。完成后打开 <http://127.0.0.1:8765>。

已有旧状态位于其他目录时使用 `academic-radar setup --from-state /path/to/old/state`。程序不会静默导入仓库内的 `state/`，以免把示例或历史数据误当成当前私有库。

Linux 和 Windows 会完成初始化与验证，但不会自动安装后台服务；请按 [部署说明](references/deployment.md) 配置用户级启动项。非技术用户可以把 [Codex 辅助安装提示词](docs/ai-assisted-install.md) 交给 Codex。

## 六个页面

- 今日推荐：今天最近一次完成导入的入选论文，默认只显示紧凑推荐信息；底部默认展示昨日推荐，可选择任意历史日期或跳转到已有更新的日期。
- 我的文献：只显示出版类型合格且达到当前阈值的论文，支持排序、筛选、收藏、导入 PDF，以及只粘贴 APA 引用和摘要的手动添加；新条目会进入下一次每日评分。
- 监测来源：实时搜索并预览可验证的期刊/会议来源，支持安全添加和移除。
- 研究兴趣：当前画像、反馈触发的更新建议和版本回退/切换；系统不会替用户生成固定提示词。
- 偏好反馈：编辑兴趣、理由、收藏和阅读状态；历史审计不作为用户模块展示。
- 更新与检查：根据当前缺口生成一份可交给 Codex 的完整更新任务。

本项目的 [云端站点](https://personal-academic-radar.thuscuf.chatgpt.site) 与本地复用同一套六页面模板、样式、脚本与图标。当前迁移版本要求网站平台与应用同时校验所有者身份；全部页面、数据读取、反馈与研究兴趣/筛选偏好编辑均仅限所有者。未配置可信所有者身份时拒绝访问，不能把知道站点链接当作访问授权。来源编辑、手动文献/摘要和 PDF 管理仍通过已支持的本地流程执行；云端迁移不会自动提供这些写入功能。

迁移前的本地主库兼容流程支持反馈双向同步：本地保存后立即安排后台同步，云端保存后立即可见并在本机在线时回传。每 5 分钟只检查变化；没有新内容时不重新上传快照。两端侧边栏都有“同步更新”：本地立即排队，云端登记请求等待下一次本机检查。同步不会启动新一轮采集或评分，完整机制见 [云端部署与同步](docs/cloud-sync.md)。

所有用户可见筛选都排除 Editorial、Corrigendum、Extended Abstract 等非正式研究内容，以及低于相关性阈值的记录。被排除记录只在内部保留最小身份和审计证据，避免反复抓取。

## 每日 Codex 流程

本地定时任务需要电脑开机且 Codex 应用保持运行。网页后台服务负责浏览，不会自行完成语义评分。可选 Sites 数据同步见 [云端部署与同步](docs/cloud-sync.md)。首页按北京时间区分今日和历史推荐；超过 36 小时没有完成评分导入，或已有新采集尚未导入时，`verify` 会提示。漏跑后 API 采集会按各来源最近成功日期扩展默认 14 天窗口。

摘要补全默认使用 180 秒预算，优先检查尚未尝试或较久未检查的记录；预算耗尽会保存进度并返回 `partial` 与 `deferred`，应继续评分导入。可通过配置的 `[enrichment] time_budget_seconds` 调整预算。官网临时网络错误会有限重试；无法核验的来源必须保留失败记录，不能将其报告为完整覆盖。

每天一次的本地 Codex 自动任务先以 14 天窗口调用元数据 API，再按已验证映射检查每种期刊截至当天的最近两期。Nature 适配器读取官网卷期与论文页；其余已映射出版商（Taylor & Francis、IEEE Xplore、ACM Digital Library、SAGE、ScienceDirect 与 INFORMS）使用出版商提交的 Crossref 印刷卷期元数据，缺失摘要时回退到 DOI 完全一致的 OpenAlex 完整摘要。这些元数据结果不代表访问或完整核验了官网页面，必须保留证据类型、来源 URL 与日期精度；仅有月/年日期时不能据此确认具体出版日。现有摘要来源优先顺序和严格导入规则不变，不能把搜索片段、Highlights 或生成式改写当作摘要。结果先严格预览、去重并原子导入，再补全摘要，最后建立一次冻结队列、逐篇判断并原子导入结果。核心命令为：

```bash
python scripts/paper_monitor.py collect-only \
  --config ~/.local/share/personal-academic-radar/config.toml

academic-radar official plan \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --output ~/.local/share/personal-academic-radar/official/plan-latest.json

academic-radar official collect-supported \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --output ~/.local/share/personal-academic-radar/official/supported-latest.json

# 按计划完成卷期证据采集并生成严格 JSON 后：
academic-radar official import \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --file /path/to/official-results.json
academic-radar official import \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --file /path/to/official-results.json --apply

academic-radar abstracts enrich \
  --config ~/.local/share/personal-academic-radar/config.toml

python scripts/paper_monitor.py agent-export \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --no-collect --batch-run <collect-only 返回的 run_id>

python scripts/paper_monitor.py agent-import \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --results ~/.local/share/personal-academic-radar/agent-results/<results.json>
```

每日默认只判断从未有过 Codex 判断记录的合格论文，包括尚未判断的采集积压；已判断的论文无论原评分高低、采用何种画像或标准，都不重复进入每日队列。更新画像、偏好反馈或摘要只影响后续新论文的判断，不自动重评旧论文。默认 `120` 是单次队列的数量上限，不是每日任务量：不足时按实际数量处理，没有候选时正常完成空队列，不用旧论文凑数；超出部分留待后续处理，`agent-export --limit` 可调整上限。

只有用户明确要求重新判断旧论文时，才在该次手动 `agent-export` 中使用 `--rescreen`；每日自动任务不得使用此参数，也不因旧标准、画像、反馈或摘要变化自行追加重评。

卷期导入要求研究论文提供完整、可追溯的原始摘要；穷尽可用证据后仍不可得时，必须显式标记缺失并记录原因，不得补写摘要。Editorial 等非研究内容可无摘要并由治理规则排除。队列与结果必须覆盖相同的全部 identity；画像哈希、反馈快照、运行编号或来源失败信息不一致时，导入会整体拒绝。我的文献只显示相关性分数大于或等于 0.70 的论文，低分记录留在内部用于去重和审计。

已验证的来源映射不等于已完成该来源的采集。没有映射、没有支持的适配器、请求失败或预算耗尽都必须显式保留；不能仅凭 API 成功报告最近两期覆盖成功。需要另行浏览器核验的来源不会自动被当作已完成。受阻卷期可使用 `academic-radar official fail` 留下可恢复的失败证据，并用 `academic-radar official status` 查看。

## 摘要补全与人工证据

```bash
academic-radar abstracts enrich \
  --config ~/.local/share/personal-academic-radar/config.toml

academic-radar abstracts export-missing \
  --config ~/.local/share/personal-academic-radar/config.toml \
  --output missing-abstracts.json
```

自动流程依次复用同 DOI 本地记录，并访问 Crossref、OpenAlex、Semantic Scholar、Europe PMC、PubMed 和出版商结构化元数据；配置 `ELSEVIER_API_KEY` 后还会使用 Elsevier 官方元数据接口补全 ScienceDirect 论文。每次尝试都记录来源、URL、时间和失败原因；缺失项可在论文卡片中手动补充完整摘要，保存后会立即更新资料。尚未判断的论文使用补全后的摘要进入每日队列；已判断论文保留原判断，只有用户明确要求时才重评。批量人工导入仍接受严格 JSON/CSV 证据包，并校验 identity、URL、重复项和明显截断内容。

## 清洗、备份与恢复

清洗必须先预览：

```bash
academic-radar cleanup preview --config ~/.local/share/personal-academic-radar/config.toml
academic-radar cleanup apply --config ~/.local/share/personal-academic-radar/config.toml --report <preview.json>
```

预览使用 SQLite 在线备份并运行 `PRAGMA integrity_check`，输出逐项身份、原因、前后统计和恢复命令。应用只更新治理状态，不删库、不抹除收藏、反馈或 PDF。

常规检查：

```bash
academic-radar verify --config ~/.local/share/personal-academic-radar/config.toml
academic-radar service status --config ~/.local/share/personal-academic-radar/config.toml
academic-radar service logs --config ~/.local/share/personal-academic-radar/config.toml
```

## 安全边界

- GitHub 只保存代码、测试、迁移和通用示例。
- 不提交 `state/`、配置、SQLite/WAL、备份、队列、结果、PDF、日志、`.env` 或任何密钥。
- 本地网页默认只绑定 `127.0.0.1`。云端迁移要求所有页面与反馈操作通过所有者授权，同步应用记录到私有访问的 D1 视图；发布凭据留在私有配置，不进入页面或 GitHub。正式迁移不得启用匿名兼容模式 `SITE_ACCESS=public-legacy`。参见 [云端部署与同步](docs/cloud-sync.md)。
- 来源配置以原子方式写入并先备份；SQLite 恢复要求显式 `--replace`，且会先保存当前库。
- PDF 保存在私有目录，校验类型和大小，并用 SHA-256 去重。

## 开发与验证

```bash
PYTHONPATH=src PYTHONWARNINGS=error python -m unittest discover -s tests -v
git diff --check
python -m build
```

系统边界与运维细节见 [架构说明](docs/architecture.md) 和 [部署说明](references/deployment.md)。

### 历史推荐

历史日期按评分导入完成时间归档，使用配置中的 `timezone`（默认 `Asia/Shanghai`），与论文发表日期、采集日期分开。同一天多次导入的历史记录按论文去重，保留该日最后一次入选时的判断；之后重新评分、调整阈值或更换画像不会覆盖过去的推荐。收藏、阅读状态和反馈与文献库同步。

数据库升级到 schema 014 后会保留已有推荐的批次和评分快照。若旧评分曾被覆盖，可用原始结果及冻结队列恢复：

```bash
python scripts/paper_monitor.py backfill-history --config ~/.local/share/personal-academic-radar/config.toml
```

恢复操作可重复执行，只补充缺失快照，不重新评分。缺少可核验原始文件时保留入选记录，并明确显示原始评分不可恢复。没有更新的日期不会标成“没有入选论文”。

### 最新推荐标准

新队列使用 evidence-v3/schema 5，逐篇保存可核验的原摘要引用，区分核心相关、具体方法借鉴、邻近背景和范围外。高分必须说明精确机制，方法借鉴封顶 0.84，邻近背景低于推荐阈值。新标准用于尚未判断的论文；旧标准记录保留原判断，不作为每日必须迁移的任务。历史推荐快照保持原貌，用户明确要求的手动重评也不会覆盖过去的推荐。

## Cloud-primary migration candidate

The optional checkpointed cloud runner, private transport and owner-only Site are described in [the migration runbook](docs/cloud-migration-runbook.md). The daily-collection parity change is an implementation candidate: online preparation journals an operation-bound API batch, freezes the latest-two-issue plan, collects each supported source, runs strict preview and atomic idempotent import, enriches abstracts, then freezes one queue with `agent-export(no_collect=True, batch_run=API_RUN)`. It preserves existing source/evidence precedence and does not run a second collection during export.

The frozen queue retains a full `collection_report`: API health, per-source issue outcomes and evidence types, and enrichment are separate. Native output and the Site snapshot status use a compact summary. Partial, failed, unsupported and budget-deferred coverage remain visible; an offline/no-collect run is `not_exercised`, never evidence of online collection success. [The native host runner](docs/native-host-runner.md) defaults to no collection; daily work must explicitly request `prepare --collect`.

`[official_collection]` defaults to `time_budget_seconds = 900`, `per_source_time_budget_seconds = 120`, and `max_sources = 20` (maximums: 900, 120 and 100). Deadlines are cooperative across worker requests and chunk reads, not a hard process kill; an active socket operation can finish after the deadline within its bounded timeout; platform DNS resolution is not a hard wall-clock guarantee. Full coverage is not guaranteed when networks stall. Retry the same saved operation, head and configuration to reuse completed work and checked evidence packages. This code does not provision credentials, enable cloud scheduling, or cut over existing jobs. Complete the runbook acceptance gates before using it with personal production state.

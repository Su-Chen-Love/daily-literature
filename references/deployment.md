# 本地部署与每日运行

## 支持边界

本地应用采用单用户本地部署：代码仓库与私有状态目录严格分离，SQLite 只由本机应用写入，网页默认监听 `127.0.0.1:8765`。本站的 Sites 版按用户授权公开全部六页面和反馈操作，持有链接的人均可浏览与保存反馈，无须登录；应用记录同步机制详见 [云端部署与同步](../docs/cloud-sync.md)。本机 SQLite 不共享；语义判断不调用独立模型 API。

## 一条命令完成设置

在虚拟环境中安装后运行：

```bash
academic-radar setup
```

该入口会：

1. 检测并升级默认私有状态，不静默读取项目内旧 `state/`；
2. 仅在显式指定 `--from-state` 且目标为空时迁移旧状态，不修改原目录；
3. 初始化缺失文件，备份并升级旧数据库；
4. 移除旧 `[llm]` 配置并保留完整配置备份；
5. 运行数据库完整性、画像、来源、语义和摘要验证；
6. 在 macOS 安装并启动可逆的用户级 launchd 服务；
7. 返回本地 URL、状态目录、配置、数据库和日志路径。

指定旧状态或暂不安装服务：

```bash
academic-radar setup --from-state /path/to/old/state
academic-radar setup --no-service
```

`setup` 与 `init` 都可以安全重复运行。目标目录已有数据时不会自动覆盖或合并另一个状态。
只有显式提供 `--from-state` 才会从其他目录迁移，避免把仓库里的示例或历史 `state/` 误当成当前私有库；迁移会自动备份、升级并验证。

## macOS 后台服务

```bash
academic-radar service status --config ~/.local/share/personal-academic-radar/config.toml
academic-radar service restart-web --config ~/.local/share/personal-academic-radar/config.toml
academic-radar service logs --config ~/.local/share/personal-academic-radar/config.toml
academic-radar service uninstall-web
```

服务仅在当前用户登录后运行，失败会自动重启；卸载只移除启动项，不删除私有状态。

## Linux 与 Windows 边界

v0.8.0 会在 Linux/Windows 完成初始化、迁移和 verify，但不会自动创建后台服务。

Linux 可创建 systemd user service，`ExecStart` 指向虚拟环境中的：

```text
academic-radar web --config /home/USER/.local/share/personal-academic-radar/config.toml
```

Windows 可为当前用户创建“登录时”任务，程序为虚拟环境中的 `academic-radar.exe`，参数为：

```text
web --config %LOCALAPPDATA%\PersonalAcademicRadar\config.toml
```

两者都必须只监听回环地址；是否启用 linger、特权账户或防火墙例外属于人工管理员决定。

## 每日 Codex 自动任务

每天 08:00（`Asia/Shanghai`）只保留一个任务，按顺序执行：

1. 用 `collect-only` 对全部已配置来源执行 14 天 API 采集，并保留这次的 `run_id`；
2. 用 `official plan` 读取每种期刊已验证的出版商卷期目录，只选择截至计划日期已经出版的最近两期；未来卷期不能占用名额；随后运行 `official collect-supported`，自动处理 Springer Nature、Taylor & Francis、IEEE Xplore、ACM Digital Library 与 SAGE。摘要按官网原文、出版商提交的 Crossref、DOI 精确匹配的 OpenAlex 顺序回退并保留 provenance，零错误后原子导入；
3. 对其余尚未成功核验的卷期逐篇打开官网论文页，复制标题、DOI、作者、日期和明确标注的完整原始摘要；按 DOI 去重，先预览、后 `official import --apply` 原子导入。已成功核验的 `issue_key` 跳过；官网受阻时用 `official fail` 写入卷期、URL 和具体原因，并以 `official status` 检查待重试项。失败时保留 API 结果，不伪造摘要；
4. 从其他可追溯渠道补全仍缺失的摘要，排除非研究出版类型并隔离证据不足项；
5. 运行 `profile review`。仅在存在尚未审阅的新反馈时判断画像是否需要调整：需要时保存一份待用户确认的完整画像建议，不需要时记录 `no-change`；不得静默替换已激活画像；
6. 用 `agent-export --no-collect --batch-run <run_id>` 将从未有过 Codex 判断的合格论文合并为一次冻结队列，包括尚未判断的积压；已判断论文不因旧标准、画像、反馈或摘要变化再次入队；
7. 用完整已激活画像和反馈样例判断每一项，再原子导入严格 JSON；界面只推荐分数大于或等于 0.70 的论文；
8. 运行 `official status` 与 `verify`，输出 API 采集、官网成功/失败卷期与论文、摘要补全、判断、入选、排除和来源失败。

默认每批最多 120 篇，是处理上限而非每日配额；不足时处理实际数量，没有候选时完成空队列，不能用旧论文补足。超过上限的未判断论文留待后续处理，`agent-export --limit` 可调整上限。每日任务不得使用 `--rescreen`；仅在用户明确要求重评旧论文的手动任务中使用该参数，历史快照仍保持原貌。

新导出会将未完成旧队列标为已放弃；导入拒绝部分、重复、画像不匹配或元数据不匹配的结果。新增期刊若有已验证官网映射会自动进入最近两期流程；否则先用 14 天 API 过渡，并在任务报告中提醒后续适配。自动任务不得上传 SQLite/WAL、配置文件、PDF、凭据或调用独立模型 API。启用 cloud_sync 后先拉取反馈、完整评分导入后同步授权的应用记录，并分别报告本地与云端结果。

## 摘要服务约束

补全器依次使用本地同 DOI、Crossref、OpenAlex、Semantic Scholar、Europe PMC、PubMed 和出版商结构化元数据。实现遵循各官方服务的限流和标识要求，缓存短期失败并支持重试。单个来源失败只记录在 `abstract_attempts`，不会中止其他来源，也不会覆盖已有摘要。

手动任务包和导入：

```bash
academic-radar abstracts export-missing --config ~/.local/share/personal-academic-radar/config.toml --output missing.json
academic-radar abstracts import --config ~/.local/share/personal-academic-radar/config.toml --input evidence.json --preview
academic-radar abstracts import --config ~/.local/share/personal-academic-radar/config.toml --input evidence.json
```

只接受明确的原始摘要证据；搜索片段、模型总结和改写文本一律拒绝作为摘要。

## 备份与恢复

```bash
academic-radar db backup \
  --db ~/.local/share/personal-academic-radar/papers.sqlite3 \
  --output ~/.local/share/personal-academic-radar/backups/manual.sqlite3

academic-radar db restore --backup /path/to/backup.sqlite3 \
  --db ~/.local/share/personal-academic-radar/papers.sqlite3 --replace
```

备份使用 SQLite 在线备份 API并执行 `PRAGMA integrity_check`。覆盖恢复前会再次保存当前数据库。不要用普通同步工具复制正在写入的 SQLite/WAL 文件。

## 故障定位

```bash
academic-radar verify --config ~/.local/share/personal-academic-radar/config.toml
academic-radar service logs --config ~/.local/share/personal-academic-radar/config.toml
```

再检查私有 `logs/` 中最近运行报告，以及 SQLite 的 `source_runs`、`task_runs` 和 `abstract_attempts`。不要把删库重建作为修复手段。

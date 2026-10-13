# Changelog

## Unreleased — 2026-10-13

- 每日默认队列只包含从未有过 Codex 判断的合格论文；已判断论文不因旧标准、画像、反馈或摘要变化自动重评。
- 明确 120 篇是单批上限而非每日配额；候选不足或为空时按实际数量完成，手动重评仅用于用户明确提出的请求。
- 保留历史推荐及当日判断快照，校正当前 Sites 全部六页面与反馈操作免登录的部署说明。

## 0.11.0 — 2026-10-09

- 新增授权的 Sites/D1 应用记录同步、本人反馈回传与 macOS 周期服务；SQLite/PDF 保留本地。
- 推荐升级 evidence-v3/schema5：原文证据、判断类别、校准封顶与分批迁移，历史快照保留。
- 修复来源分页截断误报健康、卷期跨年与仅卷号、精确 DOI 摘要核验及补全预算。
- 清洗绑定数据库与备份指纹，拒绝错库、陈旧或篡改预览；恢复规避 WAL 回放。
- 在线核验摘要溯源，清除纠错研究的勘误误标；同步状态纳入检查。
- 保存出版日期的日/月/年精度，界面与摘要报告不再把仅月份误显示为已确认日期。

All notable changes to Personal Academic Radar are documented here.

## 0.10.1 — 2026-08-29

### Changed

- Reader-facing AI judgments are now natural analytical syntheses instead of
  mechanically concatenated audit fields.
- New screening queues specify explicit depth contracts for evidence,
  profile connection, transferable value, limitations, and recommendation
  reasons.

### Fixed

- Agent import reports every invalid result in one pass, including its paper
  identity, instead of stopping at the first shallow field.
- Digest creation is staged with the database transaction so a failed import
  does not leave a misleading completed digest.

## 0.10.0 — 2026-08-11

### Added

- Evidence-structured screening with independently scored relevance, mechanism,
  transfer, evidence quality, and boundary dimensions.
- Manual abstract recovery from paper cards, with provenance and automatic
  rescreening after a successful addition.
- Optional official Elsevier Article API recovery for exact-DOI records.
- Feedback precision and error indicators in the health dashboard.

### Changed

- Recommendation reasons now expose the abstract evidence, profile connection,
  transfer value, and limitations behind each decision.
- Daily profile review uses the cumulative feedback history while retaining an
  exact unseen-event boundary for idempotent automation.
- Source coverage reports missing-abstract counts and official-issue failures.
- The status page shows only actionable failures instead of duplicating task
  history and healthy checks.

### Fixed

- Profile reviews that correctly conclude no change is needed are now visible.
- Legacy migration 012 checksums remain accepted by upgraded installations.
- The Today view falls back to the latest completed recommendation set and
  includes selected rescreen results.

## 0.9.0 — 2026-07-17

### Added

- Simple manual paper entry in “My Library” using only a Google Scholar-style
  APA citation and the full abstract, with DOI/title deduplication and automatic
  inclusion in the next Codex screening queue.
- Verified official-source support for European Journal of Operational Research
  and Transportation Science, including deterministic ScienceDirect and
  INFORMS issue metadata adapters.
- Wider, traceable abstract recovery evidence, including strict Google Scholar
  audit attempts and supported author-manuscript imports without treating
  snippets as abstracts.

### Changed

- Full-profile rescreen exports can reuse one completed collection boundary so
  all API and official-issue additions remain visible as genuinely new papers.
- Research-profile hashing now uses exact file bytes across migration, web
  confirmation, and the daily runner, including files with CRLF line endings.
- Manual title-only records retain a stable identity when a later provider
  resolves their DOI, preventing duplicate papers.

### Fixed

- Editorial-board and news/view content are consistently excluded by
  publication governance.
- Profile history labels remain contiguous after dismissed/deleted drafts.

## 0.8.0 — 2026-07-14

### Added

- Traceable, retryable abstract enrichment across same-DOI local records,
  Crossref, OpenAlex, Semantic Scholar, Europe PMC, PubMed, and verified
  publisher structured metadata, plus strict JSON/CSV evidence import.
- Publication-type evidence and allowlist governance with recoverable cleanup
  previews, verified online backups, audit reports, quarantine, and low-score
  exclusion from all normal product views.
- Live accessible source search, candidate preview, duplicate protection, safe
  source removal, background task progress, and direct retry actions.
- Compact Today/Library cards, expandable abstracts and details, persistent
  favorites, combined sorting/filtering, and one page-level PDF import dialog.
- One-command `academic-radar setup` with legacy-state detection, migration,
  verification, and reversible macOS background-service installation.

### Changed

- “Today” now means newly selected papers from the latest successful Codex
  import, not every record whose screening timestamp happens to be today.
- Feedback management is interactive while append-only history remains an
  internal recovery detail; source health is no longer a user-facing product
  concept.
- The supported product boundary is explicitly local-only, single-user, and
  private SQLite. Cloud synchronization and public/multi-user deployment are
  paused.

### Removed

- All legacy direct model-provider configuration, routes, code,
  tests, UI, and documentation. Legacy `[llm]` sections are backed up and
  removed during initialization.
- Manual ISSN/OpenAlex/query source entry and the page-refreshing
  `/sources/match` workflow.
- Repeated PDF upload forms on every card and user-facing feedback history.

## 0.7.0 — 2026-07-14

### Added

- Idempotent repair for partially upgraded legacy databases and automatic
  online backups before initialization upgrades.
- Abstract-source and coverage diagnostics, DOI-based OpenAlex repair command,
  low-priority filtering, and confidence caps when abstracts are missing.
- Journal-name candidate discovery through Crossref/OpenAlex with real metadata
  preview and cautious conference matching.
- Full-text PDF import with size/type validation, normalized filenames, SHA-256
  reuse, and correct many-paper bindings.
- Friendly server-rendered error pages and actionable Run Status checks for
  schema, integrity, profiles, source health, semantic coverage, abstracts,
  background service mode, and optional provider state.
- macOS service install/status/restart/log/uninstall commands, log archival, and
  detection when the per-user service is running a different state config.
- AI-assisted external-user installation prompt, Linux/Windows background
  service guidance, and an executable cloud/authentication/sync roadmap.

### Fixed

- Legacy states with papers but no profile ledger now repair safely on init/web
  startup instead of causing template or semantic-state failures.
- SQLite connections are closed during exceptional semantic import/export paths.
- Reinstalling a launchd service now waits for asynchronous bootout and retries
  bootstrap, avoiding transient I/O failures.
- Old service logs are archived before a new service starts so resolved
  tracebacks do not look like current failures.

## 0.6.0 — 2026-07-13

Initial public release candidate.

### Added

- Cursor-paginated Crossref, OpenAlex, and CHI collection with retries,
  provider-level degradation, DOI/title deduplication, abstract enrichment, and
  persistent source health.
- SQLite schema migrations, online backups, integrity verification, guarded
  restore, and legacy-state migration.
- Codex Automation export/judge/import flow using the host model with no extra
  model API key.
- Atomic semantic imports with complete-queue, profile-version, feedback
  snapshot, run metadata, and stale-job validation.
- Confirmed research-profile versions plus explicit draft and activation flow.
- Interested/not-interested reasons, favorites, unread/read/read-later state,
  append-only feedback history, and balanced calibration examples.
- Six-page local web application: Today, Library, Sources, Research Profile,
  Feedback, and Run Status.
- Safe source preview-before-confirmation flow and configuration backups.
- Loopback-only default binding, CSRF protection, restrictive browser headers,
  and reversible macOS launchd service management.
- Installation verification, packaging checks, unit/integration tests, and
  end-to-end operational documentation.

### Removed

- Silent heuristic fallback when no semantic model provider is available.
- API-key-dependent GitHub Actions monitoring and the obsolete direct-run
  macOS script from the supported daily workflow.

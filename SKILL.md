---
name: monitor-research-papers
description: Monitor configured academic journals and conferences for newly published papers, maintain private local SQLite history, enrich traceable abstracts, govern publication types, and use the Codex host export/judge/import workflow for relevance. Use when Codex needs to set up, run, troubleshoot, schedule, or review this local literature-monitoring workflow.
---

# Monitor Research Papers

Use the bundled deterministic runner for collection, normalization, deduplication, persistence, retry, and digest creation. In Claude/Codex scheduled tasks, use the host model itself for relevance judgment through the agent export/import workflow; this requires no separate LLM API key.

## Workflow

1. Locate the skill directory and choose a writable state directory outside it. Default to `~/.local/share/research-paper-monitor`.
2. If no configuration exists, copy `assets/config.example.toml` to `<state>/config.toml` and `assets/research-profile.example.md` to `<state>/research-profile.md`. Never overwrite an existing profile or database.
3. Refine the profile from user-provided papers, proposals, notes, and explicit feedback. Read `references/profile-guidance.md` before changing it. Preserve negative interests and boundary conditions, not just keywords.
4. Validate without changing state:
   `python3 scripts/paper_monitor.py doctor --config <state>/config.toml`
5. Start the daily API batch (14-day window by default):
   `python3 scripts/paper_monitor.py collect-only --config <state>/config.toml`
   Keep its `run_id`. Then create the verified publisher plan:
   `academic-radar official plan --config <state>/config.toml --output <state>/official/plan-latest.json`
   First run `academic-radar official collect-supported --config <state>/config.toml --output <state>/official/supported-latest.json`; preview and apply any deterministic publisher packages with zero errors. Deterministic adapters cover Springer Nature, Taylor & Francis, IEEE Xplore, ACM Digital Library, and SAGE: select the latest two already-published issues from official/publisher-deposited print metadata, prefer official or publisher-deposited abstracts, and use exact-DOI OpenAlex abstracts only as explicitly labeled fallback evidence.
   For every `browser_official` journal, inspect the latest two issues whose publication date is not later than the plan's `as_of_date`; publishers sometimes list future-dated issues first, and those must not displace the latest published issues. Skip only issue keys already recorded as succeeded. Visit each article page and capture the publisher's complete, explicitly labelled abstract. Never use search snippets, model summaries, or inferred text. Write one result entry for every planned source, preview it, and apply it atomically with `academic-radar official import`. Sources in `api_fallback` continue with the 14-day API bridge and must be reported as awaiting official adaptation.
   When a directory or article page is blocked or incomplete, persist the exact source, issue key, official URL, and reason with `academic-radar official fail`; `academic-radar official status` must then expose it for later retry. A failure entry never mutates paper data and never counts as a successful issue.
6. Enrich remaining traceable abstracts and publication-type evidence:
   `academic-radar abstracts enrich --config <state>/config.toml`
7. Check for new preference evidence with `academic-radar profile review --db <state>/papers.sqlite3`. If `needed` is false, skip profile analysis. If a pending suggestion already exists, leave it for the user. Otherwise compare every returned feedback event with the active profile. Save either a complete suggested profile with `profile suggest` or a reasoned `profile no-change`; never activate a suggestion without explicit user confirmation.
8. For a Codex scheduled task, freeze the one authoritative queue after API, official, enrichment, and profile-review work:
   `python3 scripts/paper_monitor.py agent-export --config <state>/config.toml --no-collect --batch-run <collection-run-id>`
   The default queue contains only eligible identities with no prior Codex judgment, including any never-judged backlog. Previously judged papers stay out regardless of their score, rubric, profile, feedback, or abstract changes. The default 120 is a maximum batch size, not a daily quota: process the actual count, accept an empty queue, and never fill it with previously judged papers. Report deferred never-judged candidates when the cap is reached; `--limit` changes the cap.
   Read the returned queue JSON and complete every paper using its `evaluation_policy` and the rubric in `references/profile-guidance.md`. Write a results JSON with the same `run_id` and `profile_hash`, a `model` label, and a `results` array. Each result must contain `identity`, a four-part `reasoning` object, a five-part `score_dimensions` object, `matched_themes`, `confidence`, a natural reader-facing `recommendation_reason`, three-part `study_summary`, `recommendation_type`, and exact `evidence_anchors`; the runner calculates the authoritative score. Write the reason in this order: the paper's actual problem and motivation, what the authors did, and their core finding or value. Return the same parts as `study_summary.problem_motivation`, `approach`, and `findings_value`, joined in order as `recommendation_reason`. Distinguish author-reported results from any proposed transfer to the confirmed profile; keep the decisive evidence limitation explicit. Never invent author motivation from the profile, concatenate audit labels, or paraphrase the title.
   Treat `feedback_examples` and the cumulative profile-review history as confirmed positive/negative calibration evidence,
   while keeping the confirmed profile as the primary decision rubric. Never
   change the profile from feedback without creating a draft for user approval.
9. Import the complete bounded batch and generate the digest (current schema 5 also requires recommendation_type and exact evidence_anchors):
   `python3 scripts/paper_monitor.py agent-import --config <state>/config.toml --results <results.json>`
   Use `--rescreen` only for a manual run explicitly requested by the user to revisit previously judged papers. Never add it to the daily scheduled workflow or infer rescreen authorization from profile, rubric, feedback, or abstract changes.
10. When cloud_sync is enabled, the import synchronizes automatically. Verify its result and retry with `academic-radar sync`; distinguish local import success from cloud sync success.
11. Report API collection, official issues checked/skipped/failed, inserted or updated abstracts, candidates, papers scoring at least 0.70, digest path, profile-review outcome, API-fallback sources, and source failures. Do not claim success if required sources failed or a planned official source was silently omitted.

## Safety and operational rules

- Never add a direct model provider or inspect/output environment-variable secrets. Semantic judgment is performed only by the Codex host export/import workflow.
- Default the first successful run to baseline mode: persist observed papers without emailing old backlog. Set `bootstrap_mode = "include"` only when the user explicitly wants historical results.
- Treat DOI as the preferred identity; otherwise use a normalized title hash. Upserts must be idempotent.
- For configured journals, combine the API's rolling 14-day window with the publisher's latest two already-published issues as of the run date. The issue ledger makes repeated official checks idempotent; never let a future-dated issue displace a published issue or substitute an arbitrary recent-date window for the two-issue rule.
- Prefer Crossref for DOI/publisher metadata, then OpenAlex, Semantic Scholar, Europe PMC, PubMed, and verified publisher structured metadata. Store only clearly identified original abstracts with source URL and time; never treat search snippets or generated summaries as abstracts.
- A source failure must not erase prior state or mark unseen papers as processed. Retry transient errors and continue other sources.
- Use only the host-model agent export/import workflow in Codex automations. Never silently replace semantic judgment with keyword matching.
- Judge each identity only once in the default daily workflow. Existing low scores and older rubrics are valid stored judgments, not a backlog to clear. Updated profiles and feedback calibrate future never-judged papers; updated abstracts do not authorize automatic rescreening. Preserve historical recommendation snapshots.
- Imports are all-or-nothing. They must cover every exported identity exactly
  once and preserve run, profile, feedback snapshot, and source-failure metadata.
  A newer export abandons any older unfinished queue.
- The recommendation cutoff is 0.70. Keep lower-scoring judgments in private history for audit and calibration, but omit them from the default recommendation views.
- Send email only when `delivery.enabled = true`; respect `send_when_empty`.
- SQLite/WAL, queues, results, logs, PDFs, backups, credentials, and configuration files remain private local state and never enter source repositories. When the user explicitly enables Sites synchronization, use `academic-radar sync` to exchange only authorized application records with the configured D1 service. Never copy the SQLite file or bypass access controls. See `docs/cloud-sync.md`.

## Research-interest judgment

Read `references/profile-guidance.md` whenever building or materially revising a profile. Pass the complete profile, title, abstract, venue, and publication type to the model. Require strict JSON with evidence summary, profile connection, transferable value, limitations, score dimensions, matched themes, confidence, and a separate reader-facing recommendation reason. The runner validates field depth and natural-summary constraints, reports all invalid papers together, and calculates the final score.

Favor papers with a meaningful connection to the research questions, methods, or empirical paradigms. A venue match alone is not relevance. Broad HCI work without a link to decision support, preference elicitation/integration, interactive optimization, human-AI collaboration, natural-language grounding, mixed initiative, trust/calibration, or vehicle-routing operations should normally rank lower.

## Scheduling

The skill itself performs one idempotent cycle and does not impersonate a
scheduler. Use a Codex scheduled task for the daily export/judge/import workflow
and the local web service for browsing and feedback. The supported personal
workflow does not require a model API key; see `references/deployment.md` for
backup, recovery, and service guidance.

## Troubleshooting

Read `references/deployment.md` for metadata sources, scheduling, recovery, and persistence details. Run `doctor` or `academic-radar verify` before diagnosis. Inspect the latest JSON run log in `<state>/logs/` and the `source_runs`, `task_runs`, and `abstract_attempts` tables; avoid deleting the database as a first response.

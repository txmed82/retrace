# Retrace Plan C — GitHub Code Matching + AI Fix Prompts

**Status:** Draft
**Date:** 2026-04-22
**Owner:** Colin + Retrace

## 1) Goal
When Retrace finds a bug cluster, users can connect a GitHub repo and get:
- likely code locations related to the issue
- confidence/rationale for each match
- ready-to-use prompts for AI coding agents (Codex/Cursor/Claude Code) to implement fixes safely

## 2) Non-Goals (initial)
- fully automated PR creation/merge
- automatic production deploys
- perfect root-cause detection without human review

## 3) User Stories
- As a solo dev, I connect `org/repo`, run Retrace, and get fix prompts with file paths.
- As an engineer, I review top candidate files and copy a prompt to my coding agent.
- As a lead, I track which findings got suggested fixes and which were accepted/rejected.

## 4) Product Surface (CLI-first)

### New commands
- `retrace github connect --repo org/name --branch main --token-env GITHUB_TOKEN`
- `retrace github list`
- `retrace github disconnect --repo org/name`
- `retrace suggest-fixes --report <path>|--latest --repo org/name --out ./reports/fix-prompts`

### Output artifacts
- `reports/fix-prompts/<timestamp>-<finding-slug>.md` (human-readable)
- `reports/fix-prompts/<timestamp>-<finding-slug>.json` (machine-readable)

## 5) Architecture

### 5.1 Finding Context Builder
Input from Retrace finding + signals + replay URL.
Produces normalized issue context:
- detector set
- user-flow hints (page path, click target IDs)
- likely failure class (`frontend_event_binding`, `api_4xx`, `api_5xx`, `validation`, etc.)
- query terms for code search

### 5.2 Repo Connector
Initial implementation options:
- MVP: local repo path + optional `gh`/API metadata using token
- Next: GitHub App OAuth flow

Stores repo identity and default branch in local SQLite.

### 5.3 Code Indexer (MVP)
Index lightweight repository metadata:
- file path
- extension/language
- exported symbol names (regex-level first)
- route/component hints (path segments like `routes`, `pages`, `api`, `controllers`)

No heavy AST at first; use fast lexical index + heuristics.

### 5.4 Matcher
Hybrid scoring per file/symbol:
- lexical score from finding text/signals (`rg`/BM25-style)
- structural score (route/path overlap)
- detector prior score (e.g., `network_5xx` boosts backend handlers)
- optional git ownership score (recently changed files touching flow)

Output top-N candidates with explanation.

### 5.5 Prompt Generator
Generate agent-specific prompts with:
- bug summary
- evidence
- candidate files (ranked)
- suggested implementation strategy
- required tests and acceptance criteria
- constraints (don’t break existing flows, preserve API contract)

Prompt variants:
- `codex_worker`
- `cursor`
- `claude_code`
- `generic_openai`

## 6) Data Model Changes (SQLite)

Add tables:
- `github_repos(id, repo_full_name, default_branch, remote_url, provider, connected_at)`
- `report_findings(id, report_path, finding_hash, title, severity, category, session_url, created_at)`
- `code_candidates(id, finding_id, repo_id, file_path, symbol, score, rationale_json, created_at)`
- `fix_prompts(id, finding_id, repo_id, agent_target, prompt_markdown, prompt_json, created_at)`

Notes:
- `report_findings` decouples suggestions from in-memory pipeline output.
- Use stable `finding_hash` to dedupe reruns.

## 7) Implementation Phases

## Phase 0 — Design + Scaffolding (1-2 days)
- Add command groups and config placeholders.
- Add DB migrations and table access methods in `storage.py`.
- Add report parser for existing markdown findings.

Exit criteria:
- Commands run and store/retrieve connected repo + parsed findings.

## Phase 1 — Matching MVP (2-4 days)
- Implement lexical matching over repo files.
- Add detector-to-code-domain priors.
- Return top 5 files with score + rationale.

Exit criteria:
- For at least 60% of sampled findings, top-5 includes a human-validated relevant file.

## Phase 2 — Prompt Generation MVP (1-2 days)
- Add prompt templates for Codex/Cursor/Claude Code.
- Emit markdown + JSON prompt artifacts.

Exit criteria:
- Engineer can copy prompt and get a meaningful first patch in coding agent.

## Phase 3 — Trust + Workflow (2-3 days)
- Add confidence buckets and explicit uncertainty text.
- Add review-state tracking (`accepted`, `rejected`, `needs-investigation`).
- Improve docs and examples.

Exit criteria:
- Dogfood loop can track suggestion usefulness and false positives.

## 8) File-Level Work Plan
- `src/retrace/cli.py`
  - register new `github` and `suggest-fixes` commands
- `src/retrace/storage.py`
  - schema + CRUD for repos/findings/candidates/prompts
- `src/retrace/commands/github.py` (new)
  - connect/list/disconnect command handlers
- `src/retrace/commands/suggest_fixes.py` (new)
  - report parsing, matching orchestration, prompt generation
- `src/retrace/matching/` (new)
  - `indexer.py`, `scorer.py`, `models.py`
- `src/retrace/prompts/` (new)
  - prompt templates and renderers
- `tests/test_suggest_fixes.py` (new)
- `tests/test_matching_scorer.py` (new)
- `tests/test_storage_github_tables.py` (new)

## 9) Acceptance Criteria (MVP)
- User can connect at least one repo and list connections.
- Given latest report, `suggest-fixes` generates candidate code files and prompts.
- Generated artifacts include confidence, rationale, and test expectations.
- End-to-end command completes in under 30s on a medium repo (<5k files) without embeddings.

## 10) Risks and Mitigations
- Weak file matching on large repos:
  - start with top-N + transparent rationale; add embeddings in v1.1
- Sensitive data in prompt artifacts:
  - redact secrets/tokens; avoid dumping raw env values
- Overconfident bad suggestions:
  - enforce uncertainty language and evidence sections
- GitHub auth complexity:
  - MVP uses PAT/local repo path first; GitHub App second

## 11) Dogfood Plan (Cerebral Labs)
- Run `retrace` daily and generate fix prompts for each new report.
- Track per finding:
  - `prompt_used` (yes/no)
  - `patch_opened` (yes/no)
  - `fix_merged` (yes/no)
  - `suggestion_quality` (1-5)
- Weekly tune scoring weights based on misses.

## 12) Open Decisions
- Should MVP require a local checkout path or clone automatically?
- Do we emit GitHub issue markdown immediately or only agent prompts?
- For matching quality, do we prioritize speed (lexical only) or add optional embeddings in MVP?

## 13) Recommended Next Step
Implement **Phase 0 + Phase 1** first (repo connect + lexical matcher), then start Cerebral Labs dogfooding before adding GitHub App OAuth.

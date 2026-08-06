# Grilling Session — Project Plan Decisions (2026-08-06)

Relentless interview over the user's 3-part project plan (GitHub collection+AI analysis,
HuggingFace hot/trending models, multi-platform news with human review). All 13 decisions
below are **locked** by the user. This document is the source of truth for the next
implementation phase and should be reconciled into ROADMAP.md + CONTEXT.md + ADRs.

## Context discovered (facts from the codebase, not assumptions)

- Layered arch exists: `collectors/` (GitHub + GitHubTrending + HuggingFace + News),
  `analyzers/`, `ai/` (ProjectSummarizer pluggable ollama/openrouter/openai + BatchSummary),
  `services/`, `bot/` (aiogram, github/hf/news handlers), `scheduler/jobs.py` (daily 8:00).
- 4 ADRs accepted: 0002 unified ContentItem+MetricSnapshot (category removed from snapshot key);
  0003 dual-channel (channel=reviewed news only, bot push=GitHub/HF); 0004 channel discipline
  (only reviewed news cards, comments via Telegram discussion group).
> NOTE: ADR-0001 (`docs/adr/0001-publishing-and-review-pipeline.md`) was ALREADY fully
> written (Status: Accepted, 2026-08-06) before this session. It pre-decided the review
> gate, dual-channel, HF trend, dedup, full-text caching, failure state machine, and
> provider consolidation. This grilling session CONFIRMED those decisions and only fills
> gaps ADR-0001 left open (storage migration, schema split, renderer, tests, cadence).
> Where this doc and ADR-0001 disagree, ADR-0001 wins.

- **Contradictions found vs ADRs (the real code problems):**
  1. ADR-0002 NOT implemented: both collectors still use
     `index_elements=["source","external_id","category","captured_date"]` (github_collector.py:283,
     huggingface_collector.py:138). `category` still in the key. Unified storage is doc-only.
  2. HF has NO "update trend" path — only `fetch_by_category` (hot/by-pipeline). User wants
     hot + trending. ADR-0001 §4 already decided `lastModified` sort + `metrics.search_type`
     split, but ZERO code exists.
  3. Review gate has ZERO code (ADR-0001 §1–3 is decided, not built):
     `DailyPublisher.publish_to_channel` is `pass`, no review/approve/draft in the repo.
  4. `publisher.py` is stale: hardcodes `provider="openrouter"`, reads old fields
     (`project.summary`, `project.stargazers_count`) that don't match unified `ContentItem.metrics`.
  5. News accuracy has no design yet beyond ADR-0001 §6 (full-text cached in `raw_data`).
     The Q3 "no misquote" guarantee (prompt constraint + `source_quotes`) is not in ADR-0001.
  6. `UserFavorite.project_id` vs `huggingface_service.item_id` mismatch (CONTEXT.md flagged).

## Locked Decisions (Q1–Q13)

| # | Topic | Decision |
|---|-------|----------|
| Q1 | Storage unification (ADR-0002) | **A** — Complete migration FIRST (drop `category` from snapshot key, fold historical rows, fix `UserFavorite` field), before new features. |
| Q2 | HF "update trend" definition | **A** — Dual path: *hot* = pipeline category + `trendingScore`/`downloads`; *trend* = `lastModified` desc + `MetricSnapshot` 7-day growth (`search_type="hf_trend"`). |
| Q3 | News accuracy (no misquote) | **A** — Per ADR-0001 §6: fetch full text at COLLECT time, cache in `raw_data`; AI reads cache only. Plus Q3 guarantee: prompt hard constraint (verifiable statements only, no inference) + `source_quotes` (2–3 verbatim excerpts) + review gate rejects any summary sentence with no supporting excerpt. |
| Q4 | Human review gate (ADR-0001) | **A** — Telegram review: post cards to a private review group with ✅/❌ inline buttons; ✅ forwards to public channel (discussion group → comments isolated, fits ADR-0004). |
| Q5 | GitHub/HF bot push trigger | **A** — Scheduled push (e.g. 09:00) + on-demand commands (`/github trending`, `/hf hot <cat>`, `/subscribe`) + `Subscription` table management. |
| Q6 | Shared AI summary schema | **B** — News uses its OWN schema, not mixed into project schema. News adds `source_quotes`, `source_url`; drops `install_step`/`pros`/`cons`. |
| Q7 | Failure / empty-data strategy | **A** — Per ADR-0001 §7: `processed` state machine 0/1/2/3 + backoff retry + lightweight metrics + `/status` endpoint. Single source/item failure doesn't break the batch; failed items marked `processed=error` + skipped; empty data skipped silently. |
| Q8 | LLM provider + cost | **A** — Per ADR-0001 §8: consolidate provider selection (all read `settings.default_llm_provider`, delete publisher hardcode) + cost/token tracking + fallback consistency BEFORE wiring push. News forced to cloud; `TokenCost` tracked; daily budget cap → degrade + alert. User supplies API keys via `.env.local`. |
| Q9 | Collection cadence | **A** — Full collect daily 08:00; lightweight metric snapshot (stars/likes/downloads) every 6h WITHOUT AI; news daily 1x. |
| Q10 | Deduplication | **A** — Per ADR-0001 §5: exact `exact_url` match + title similarity (Levenshtein ≥ 0.85) → `NewsCluster` (canonical + members). Embedding semantic clustering is **v2** (deferred). |
| Q11 | Render framework | **A** — Unified `Renderer.render(item, schema)` interface; per-source impl; reuse/extend `NewsProjectRenderer.card`. Handles escaping, truncation, buttons. |
| Q12 | Testing strategy | **A** — Layered: collector unit (mock API→assert save fields), schema contract (news has `source_quotes`, JSON-retry), dedup unit, pipeline smoke (SQLite in-memory end-to-end). `/health` as acceptance gate. |
| Q13 | Execution order | **A** — Phase 1 (wk1): Q1 storage migration + Q7 failure isolation/`/health` + Q8 provider/cost wiring. Phase 2 (wk2): Q2 HF trend + Q5 bot push + Q11 renderer. Phase 3 (wk3-4): Q10 dedup + Q3 accuracy + Q6 news schema + Q4 review gate + Q9 cadence. Phase 4 (wk5): Q12 tests + hit ROADMAP targets. |

## First Milestone (end of Week 1 — must be verifiable)

1. Migration: `MetricSnapshot` unique key drops `category`; historical multi-rows folded;
   `UserFavorite` reconciled to `ContentItem.id`. Both collectors upsert single row per entity.
2. `/health/pipeline` endpoint returning per-source last-run success rate + item counts.
3. All `Summarizer` calls read `default_llm_provider`; `publisher.py` hardcode removed;
   `TokenCost` recorded per summary.
4. Single-source failure isolation verified by pipeline smoke test (Q12).

## Open follow-ups (not decisions, need separate sessions)

- Embedding service choice for Q10 v2 semantic layer (ADR-0001 §5 defers it; Levenshtein≥0.85 is v1).
- Subscription granularity UI for Q5 (`/subscribe` menu structure).
- Budget cap threshold number for Q8 (needs a concrete dollar/day value from user).
- Confirm `processed` state-machine semantics (0/1/2/3 per ADR-0001 §7) against `ContentItem.processed` (currently 0/1 only).

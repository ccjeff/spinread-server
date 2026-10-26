# Chapter analysis and comparable training review

Select a chapter and **分析本段**. Choose the target player and optionally a focus. The service supplies credentials; the browser neither requests nor submits provider keys or a provider-consent checkbox. The panel shows observations, replay evidence, actionable practice suggestions, and an optional historical comparison. Sampling diagnostics, model versions, tokens and cost remain internal and are excluded from public analysis responses.

## Input contract

`spinread/product/chapter_contract.py` defines the validated `ChapterInput` (`chapter-analysis-v1`): scope with video/chapter IDs and time/version bounds, a user-selected target with provenance, human training intent with an ordered action sequence, focus, manifest reference and known limitations. `snapshot.input` stores the exact contract passed to both model calls. The record's `manifest` is addressed as `<analysis_id>:samples`. An analysis target override never inherits another player's annotation. Missing intent is explicitly UNKNOWN, never inferred from a chapter title.

Frame manifests record video/role, actual decoded timestamp, window, original/resized dimensions and crop rectangle. Storyboard frames also record sheet, row, column, image rectangle and canvas dimensions. The stored strategy version defines deterministic resizing. Actual image binaries are temporary and are not retained.

## Actionable output

The final schema adds `practice_plan[]`: goal, zero-based observation indices, evidence IDs, drill, ordered steps, proposed dosage/rest, cue, observable success criteria and retest conditions. Assessable reports require 1–3 evidence-backed suggestions; NOT_ASSESSABLE reports cannot invent a practice plan. Suggestions are AI drafts for the player and coach to adjust, not published schedules or numerical ability scores.

## Historical comparison

`GET /api/videos/{id}/chapter-comparison-candidates?annotation_id=...&target=...` finds other non-deleted videos owned by the requester with an exact normalized match of feeding, movement and ordered actions (hand/stroke/manual spin/movement). Both chapters need explicit action annotations and an unambiguous near/far target. A title or near/far position alone is never used to match identity. Different positions between recordings are supported.

The optional comparison selector is explicitly for the same player. Selecting a chapter asserts that identity; no face recognition is implied. The create request includes `{video_id, annotation_id, annotation_version, same_player: true}`. The service revalidates ownership, versions, annotation match, range and media availability. Matching is conservative: incomplete or synonymous annotations can yield no candidates; this is shown as an actionable empty state.

The worker reads actual frames from both videos, using the same three-window/4 fps overview strategy and distinct CURRENT/PREVIOUS frame IDs. The model assesses visual comparability instead of treating matching labels as proof of equal difficulty. Each change must cite both sides. LIMITED permits only UNCERTAIN; NOT_COMPARABLE must have no changes. No previous input means comparison=null. Upload order does not establish recording chronology or training effectiveness.

History is snapshotted in the cache identity. Deletion/revoked ownership is checked before each paid call and publication. Read access to the current video does not grant access to the historical one: a combined result is withheld if the reader cannot access that source. Annotation/media/timeline revisions mark results stale. Evidence buttons replay the current video or open a bounded historical player inside the panel.

## Runtime and API

Configure `SPINREAD_OPENAI_API_KEY` (or `OPENAI_API_KEY`) for the API and worker. It is a masked SecretStr setting, never stored in jobs, business records, responses or browser state. Local `.env` is ignored by Git; production should inject a service secret. The temporary BYOK vault has been removed. Embedded or separate workers use the same configured credential. Existing pre-v2 queued tasks without a contract fail without sending frames and can be resubmitted.

- POST `/api/videos/{id}/chapter-analyses` requires Idempotency-Key and the chapter selection; no key, consent or budget fields. Budget is server-controlled at $0.15/task. Same-key replay, source-aware cache reuse and explicit force-reanalysis remain supported; only one active analysis per video.
- GET collection/item returns product-facing status, target/focus, result, minimal replay manifest and authorized comparison source. Detailed input, input/coverage limitations, usage, cost, coverage and strategy are retained in the database only.
- POST item `/cancel` stops subsequent stages. An in-flight call may still complete; paid calls are never automatically retried.

Pinned model: gpt-4.1-mini-2025-04-14; strategy: chapter-visual-v2.1. Two calls maximum, store=false, strict output schema. Overview: up to three six-second windows per video, 4 fps, three-column sheets of up to 12 frames. Optional detail: only CURRENT, at most two nonoverlapping two-second windows at 8 fps. Historical detail is not requested; insufficient historical visibility lowers comparability. Output limit: 5,000 tokens/call. Before each call a conservative bound enforces the task budget. Internal rates remain $0.40/$1.60 per million input/output tokens; provider billing may differ. No audio or whole-video file is uploaded.

Run the existing 0007 migration if needed; this revision requires no new tables. Restart API/worker after changing configuration. Schema and reference validation cannot establish coaching correctness; user and coach review are still needed.

## Verification

Tests use isolated SQLite data and mock provider calls, including two distinct video downloads, the exact outgoing input contract, target overrides, conservative matching, same-player selection, authorization, source deletion, strict response schema, executable plans, two-sided citations, cancellation, budget and paid-usage retention. Real-frame extraction tests check manifest geometry. No production annotations are changed to create comparison fixtures.

See [SpinRead LLM strategy](<SpinRead LLM strategy.md>) for the wider roadmap. Whole-session reports, individual rally analysis, long-term growth pages, calibrated scoring and coach review controls remain separate work.

Verified on 2026-09-29: 46 related backend tests and 12 frontend navigation tests pass; production frontend build passes. A real CB9 first-chapter run with strategy v2.1 completed and returned an evidence-linked drill with steps, dosage/rest, cue, concrete success criteria and retest conditions. The first live v2 trial was rejected by the evidence gate; supplied-frame enums and citation-count bounds were then added to the provider schema. This is functional verification, not coach validation of technical accuracy. Current production annotations contain no exact cross-video action match, so historical matching/evidence/access paths were verified with isolated fixtures rather than modifying real annotations.

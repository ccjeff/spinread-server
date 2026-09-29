# Single-chapter visual assessment

Select a training chapter in the video detail page and choose **分析本段**. Select the near/far player, optionally enter a focus, enter an OpenAI API key for this task, and approve sending sampled frames and the chapter context. Results appear beside the video with clickable evidence. The API key is cleared from the form after submission and is never written to browser persistent storage, the job payload, logs, or assessment records.

## Runtime

Run `alembic upgrade head` and restart the API after installing the project dependencies. This increment supports **one API process with its embedded worker** (`SPINREAD_EMBED_WORKER=true`). It does not support independent/multiple worker processes for BYOK jobs: credentials are held in a bounded, 15-minute, process-local vault. Queue payloads contain only the analysis ID. A restart loses the credential and the job fails clearly when claimed; a stale status expires after 15 minutes on read. Do not run a separate worker against this queue for this feature. Distributed execution requires a scoped secret broker before rollout.

The normal workflow makes two Chat Completions requests to pinned `gpt-4.1-mini-2025-04-14`, with `store=false` and strict JSON schema. It does not upload the video or audio to OpenAI. Provider retention rules still apply. The API uses the existing owned-video permission for creation/cancel and the existing video/coach grants for reading.

Sampling v1: up to three uniformly distributed six-second windows at 4 fps, presented in time-labelled 3-column sheets of at most 12 frames. Overview may request at most two nonoverlapping intervals inside the selected range, each at most two seconds, sampled at 8 fps as independent images. Out-of-range/duplicate/excess requests are not executed. The proxy is decoded locally; actual decoded timestamps are recorded, duplicate/out-of-range frames removed. Overview requests are bounded rather than free-form tools. No pose/trajectory claims are fabricated when such inputs are unavailable.

The worker downloads the existing normalized proxy to a temporary working directory and removes that directory on exit. It retains the frame manifest and coverage, not generated image binaries. A report is an AI draft, not a coach-approved result or numerical score. Existing labels, events, quiz answers and reports are unchanged.

## API

- `POST /api/videos/{id}/chapter-analyses`: requires `Idempotency-Key`, `X-OpenAI-Key` and a scoped request with `consent=true`. Same request key/content replays; mismatched content returns 409. A source snapshot includes media, timeline, annotation, context, target and model/strategy versions. Identical completed inputs reuse a result unless `force=true`; at most one active analysis per video.
- `GET /api/videos/{id}/chapter-analyses`: latest 50 records, including old source revisions.
- `GET /api/videos/{id}/chapter-analyses/{analysis_id}`: status, input snapshot, coverage, evidence manifest, result and usage.
- `POST /api/videos/{id}/chapter-analyses/{analysis_id}/cancel`: stops subsequent stages. An already sent provider request can still complete and incur charges; its recorded usage is retained.

The UI caps each task at $0.15 USD. Before each call, the backend computes a conservative input/output estimate against the remaining budget. Pricing is versioned in the adapter at $0.40 / $1.60 per million input/output tokens, verified 2026-09-28; estimates ignore cached-input discounts. The displayed cost is calculated from returned usage, not a guaranteed invoice amount. Timeouts can be billable without returned usage. Paid calls are never automatically retried. Update model pricing/capabilities before changing the adapter.

Result validation checks the schema, the maximum number of observations and improvements, assessment consistency, and references to frames actually provided. These checks do not establish the correctness of the coaching judgment; that requires user/coach review. Source changes mark old results stale rather than modifying them.

## Validation

- Isolated API/worker tests cover ownership, deleted media, source conflicts, cache/idempotency, secret separation, expired credentials, cancellation during provider calls, bounded detail requests, invalid evidence, provider-error redaction, usage retention and real frame extraction.
- Related backend regression suite: 36 passed. Frontend navigation suite: 12 passed; TypeScript/Vite build passed. Existing lint warnings remain outside the new panel.
- Live smoke test: CB9DBF88-49E7-48A8-BCDD-07B63771076E.mov, first manual chapter (2.991–564.090 s), far player. Two real calls, 71 overview frames, no detail requested by the model; 26,802 input tokens and 528 output tokens, estimated $0.011567 USD. This is a workflow check, not an accuracy evaluation. The bounded detail branch was exercised by isolated worker tests.
- Browser check: independent chapter settings, history retrieval, evidence seeking and short replay auto-stop.

Overall strategy: [SpinRead LLM strategy](<SpinRead LLM strategy.md>). Later phases include individual-rally analysis, coach review controls, calibrated scoring, whole-session summaries and longitudinal comparisons.

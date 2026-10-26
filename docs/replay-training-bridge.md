# Replay, receive practice and coach guidance

The integration appends optional replay evidence and human guidance. It never replaces source videos, active timelines, quiz attempts, existing training tasks or retest snapshots. Migration `0009_replay_training_bridge` adds two tables and one nullable task reference; existing rows need no backfill.

## Replay and landing statistics

`GET /api/videos/{video}/rallies/{rally}/replay` accepts `timeline_version`, `start_ms` and optional `end_ms`. A response covers at most 30 seconds in the active rally. Pose frames, 3D ball segments, biomechanics, strokes and placements use the same original millisecond clock. The latest successful supplemental analysis for the exact timeline is preferred; the original EVENTS artifact remains the fallback.

Automatic placements remain `CANDIDATE`. Statistics expose human `confirmed_count` and `phases` separately from `model_estimates`. Unknown table positions or unresolved lateral/depth categories are excluded from the grid denominator and counted in `unknown_count`.

`all_placements: {sample_count, grid}` totals valid human-confirmed placements across phases. `model_estimates.all_placements` totals valid non-rejected local calibrated and upstream placements, including those whose phase is still `UNKNOWN`. `unclassified_count` (also under `model_estimates`) counts those valid but unclassified positions. The existing phase-specific model grids retain their upstream-only meaning; the server does not guess serve/receive/rally labels for local estimates. Rejecting a candidate removes it from the aggregate; explicit owner confirmation adds it to the selected human-confirmed phase.

`PUT .../replay-review` echoes `base_version`, `timeline_version` and `analysis_artifact_id`. A new supplemental analysis invalidates previous landing confirmations in the current view while preserving their stored record until an explicit new review. Imported placements can be reviewed with `corners: null`; manually calibrated candidates require four corners and `fixed_camera: true`. Partial/unknown placements cannot be promoted to complete confirmed statistics. Only the owner edits landing reviews; authorized coaches can inspect them.

For camera changes, GET and PUT support `calibrations: [{id, corners, start_ms, end_ms}]` with at most 100 non-overlapping, half-open windows. Each quadrilateral must be convex, normalized and non-degenerate; each range stays inside the video. New or changed windows require `fixed_camera: true`. The detector slices both the observed frames and active contacts to each window before evaluating its five-frame bounce neighborhood, so a camera cut cannot contribute geometry from the neighboring view. Local placement candidates include their `calibration_id`.

Clients send the full calibration list and retain stable ids when editing. A changed or removed window invalidates reviews only in the union of its old and new bounds. Clients must omit those affected reviews (`422 CALIBRATION_CHANGED` otherwise); the server automatically preserves unrelated reviews even if the request's review list is empty. It does not revive reviews invalidated by a timeline or analysis replacement. Legacy records are read as an `id: "legacy"` window without a database rewrite. Old top-level corners/range fields mirror the first window, and legacy writes edit only that first window while retaining the rest. With an explicit `calibrations` list, the list governs geometry and the legacy fields can be omitted.

## Draft practice from replay

`POST /api/videos/{video}/replay-quiz-candidates` with `Idempotency-Key` and `{timeline_version, rally_ids?: []}` generates at most 300 draft clips from active timeline contacts. The first two contacts provide a proposed serve and pause boundary, not confirmed serve recognition. The owner reviews and approves boundaries through the existing quiz revision endpoint. Repeated generation reuses existing current items and never replaces a reviewed item or attempt history.

`GET /api/quiz-items/{item}/evidence` requires an attempt by the owner for that exact item revision. It returns a bounded replay and immutable coach reference answers. It rejects stale items and never includes a next-page pointer outside the quiz. Quiz list/recommendation responses contain no reference answers or post-pause analysis. This is a practice reveal policy, not a restriction on the owner's ordinary access to their original recording.

## Structured coach guidance

The review detail includes quiz ids, item versions, timing, current rally ids and stale flags in `practice_notes`. The coach can submit:

```
POST /api/review-requests/{request}/quiz-reference-answers
Idempotency-Key: <unique request key>
{
  "base_version": 1,
  "quiz_item_id": "quiz_...",
  "quiz_item_version": 2,
  "timeline_version": 1,
  "answer": {
    "spin": "UNKNOWN",
    "length": "SHORT",
    "acceptable_receives": ["PUSH", "FLICK"],
    "note": "Coach's explanation"
  },
  "evidence": {"rally_id": "item_...", "contact_ms": 1200, "replay_artifact_id": "art_..."},
  "diagnosis": "EXECUTION"
}
```

Both the relationship grant and per-video consent remain required, including for idempotent retries. The quiz must already be attempted and approved; the review, quiz and active timeline versions must agree. Evidence must point to an active contact in the same rally and clip. The client echoes `practice_notes[].replay_artifact_id` (nullable when no artifact exists); a same-timeline analysis replacement returns `409 REPLAY_EVIDENCE_CONFLICT` until the coach refreshes and inspects the new evidence. Every submission creates an immutable answer version and increments the review version. Diagnosis is `READING`, `EXECUTION`, `MIXED` or `UNKNOWN`; multiple receives may be acceptable. No attempt is automatically scored, and the coach may retain unknown spin.

`POST /api/training-plans/items` additionally accepts `reference_answer_id`. The server validates that the answer belongs to the current quiz/timeline of the task's report and records its immutable id in `PlanItem.quiz_reference_answer_id`. Existing coach locks, task history, player notes, permissions and retest comparability gates remain in force.

Validation: `tests/test_replay_training_bridge.py`, `tests/test_replay_bridge_migration.py` and the existing quiz, replay and training-loop contracts run against isolated SQLite databases and synthetic evidence. They do not touch the live video store.

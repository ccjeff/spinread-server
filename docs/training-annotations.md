# Human training chapters

GET /api/videos/{id}/training-annotations returns video_id, version, segments and updated_by; no record is version 0. Reads require existing video access. PUT accepts base_version and the complete nonoverlapping segments array; writes require ownership. Video row locking and version checks prevent lost updates (409).

Each segment has id, start_ms/end_ms, title, feeding (RALLY/MULTIBALL/SERVE_RECEIVE/OTHER), movement (FIXED/TWO_POINT/MOVING/UNSPECIFIED), target, ordered actions (hand/stroke/incoming_spin/movement), and notes. Ranges must be positive, within video duration and nonoverlapping; IDs must be unique. Tiny retained edge fragments are allowed; the UI requires a new selection of at least one second.

Migration 0006_training_annotations adds append-only training_annotation_revisions with author and timestamp. Records are independent of generated timeline IDs. Every save emits an audit event; historical revisions remain when an annotation is removed. No pipeline, quiz approval, media asset or original rally/contact record is modified.

Frontend range replacement clips overlapping old annotations, preserves outside remainders, and overlays manual chapters over automatic navigation. A crossing rally is assigned once by maximum overlap while its timestamps remain intact. This is human training-context data, not verified per-stroke labels or classifier ground truth.

Validation: isolated API tests cover authorization, deletion access, invalid/overlapping ranges, duplicate IDs, stale versions, history, and timeline regeneration. Frontend tests cover range replacement, retained fragments, empty automatic timelines, crossing rallies and time parsing. Live UI verification uses the user-confirmed 38:00–51:27.457 serve/receive interval; experimental action edits are cancelled.

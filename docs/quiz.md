# Private serve quizzes

SpinRead supports a player–AI–human coach training loop. This increment prepares evidence and records the player's judgments and questions for later coaching. It does not implement coach sharing or claim coach approval.

## Run

Install the sibling analysis library (`pip install -e ../pingpong-training`), run `alembic upgrade head`, and restart the API/worker. The new `QUIZ_GENERATE` handler runs asynchronously on the existing PostgreSQL queue; an old worker must not remain running after the update.

In the web app, open a ready video → 发球练习 → 寻找发球候选. Review candidates or manually set start, estimated serve contact, pause-before-receive, and continuation end. Confirm the visual boundaries to add an item to the private practice pool. This confirmation does not confirm a spin/receive answer.

Practice: play the serve → pause → answer spin/length/receive and confidence, optionally record a question for a coach → submit → watch the real continuation. Answers are persisted; unattempted current items are recommended first. Historical attempts remain available after a boundary revision.

## API

- `GET /api/videos/{video_id}/quiz-items`: private candidates, current timeline version, generation status and attempted flags.
- `POST /api/videos/{video_id}/quiz-candidates`: idempotent asynchronous generation per video, timeline and detector version. A failed generation may be explicitly retried.
- `POST /api/videos/{video_id}/quiz-items`: manually create a private draft or boundary-confirmed observation item.
- `POST /api/quiz-items/{item_id}/revisions`: immutable new revision; requires `base_version` and current `timeline_version`. Old attempts retain the original item reference.
- `GET /api/quiz-sets/recommended?video_id=...`: up to 200 approved current items; optional video filter. Unattempted items first.
- `POST /api/quiz-items/{item_id}/attempts`: requires `Idempotency-Key`, item version, answer, confidence (1–5) and elapsed milliseconds. Reusing the key with different content returns 409.
- `GET /api/quiz-items/{item_id}/attempts`: latest 50 personal attempts across that item's revision family.

All routes enforce video ownership and deletion status. Source timeline changes make old items stale immediately at read/submit time; they are excluded from practice until explicitly reconfirmed against the current timeline. A revision never edits the original bounds or historical answer.

## Practice classifier

The quiz flow separates practice-type classification from individual serve-boundary proposals. `practice-av-1` extracts non-overlapping 20-second contexts: mean, standard deviation, 20th/80th percentiles of grayscale motion in two image halves, plus onset density, median gap, gap variability and reset fraction. These are broad spatial features, not player/pose or ball tracking.

`practice-centroid-1` is a supervised recording-local nearest-centroid baseline, standardized with a scale floor. Classes are RALLY, SERVE_RECEIVE and OTHER; UNKNOWN is explicit abstention. Only reviewed context labels train the classifier. At least three usable examples of each class are required. Missing visual features, short tails, close competing classes (distance margin < 0.25), and out-of-distribution distances abstain. The margin is not a calibrated probability. No generic pretrained model or cross-video accuracy is claimed.

`PracticeWindow` persists features, feature version, reviewed label/source, revision and an append-only review log. `GET /quiz-items` includes contexts, model readiness/counts, predictions and candidate context. `POST /videos/{video_id}/practice-windows/{window_id}/review` accepts current timeline version, review revision and label (null withdraws a review). Access is owner-only. Concurrent/stale reviews return 409. Training examples never cross recording, owner or timeline boundaries; predictions never feed themselves back into training. Changes affect suggestions, not approved quiz revisions or attempts.

The UI defaults to SERVE_RECEIVE proposals and exposes RALLY/OTHER/UNKNOWN filters. Reviewers can play a full context, correct its class and immediately refresh other predictions. Classification alone never approves a quiz. Explicitly approved clips remain available even if surrounding practice context changes. On a new recording without reference labels, suggestions remain UNKNOWN until enough examples have been reviewed. The current development recording has nine provisional assistant-reviewed reference contexts (three per class), clearly distinguished from user and future coach review. Their data remain in the local database, not in Git.

Validation so far covers extraction, trained classification, abstention, review permissions/conflicts, isolation, and preserved quiz history. A small visual spot check withheld three contexts from fitting: one received the expected class and two abstained. This is not an accuracy estimate. Before enabling automatic approval or a shared model, build a coach-reviewed dataset, split evaluation by recording/session (not neighboring windows), and measure class precision/recall, coverage/abstention, and serve-candidate precision separately. Background-table motion/audio, occlusion, camera changes and mixed drills remain limitations.

## Candidate detector and limits

`serve-practice-context-0.2.0` prepares audiovisual practice contexts and examines ALL audio onsets from the original media cache, independently of the existing activity/rally minimum duration and hit-count filters. It coalesces close peaks, looks for a quiet reset followed by plausible impacts, and proposes a preparation window and continuation. The initial pause is a provisional offset, not a detected receive event. Confidence is unknown. Every generated item is DRAFT and requires visual confirmation. It can mistake pickups, adjacent-table audio, or ordinary rally restarts for serves and can miss serves; no detection-accuracy claim is made. Missing audio or no candidates leaves manual creation available. Generation retains at most 300 candidates with an explicit truncation notice.

Intervals are source milliseconds: `0 <= start < contact < pause < end <= duration`, with a 30-second maximum manual clip. The web practice player uses bounded virtual playback and stops slightly before the pause boundary, with native seeking disabled. It is an observation-training interaction for the owner, not a DRM or examination security boundary; the owner can still access their original video. Exported/materialized quiz-only delivery can be added separately.

All items in this release are unscored. No model-generated or guessed spin/receive answer is promoted into a standard answer. `answer_basis` and provenance are retained for later coach-confirmed feedback; actor/role authorization and sharing must be implemented before enabling coach or public pools.

## Validation

- API tests use an isolated SQLite database: ownership/deletion, bounds, drafts, immutable revisions, stale timelines, attempt retries, unknown answers, generation replay/failure/retry.
- Existing server integration suite exercises PostgreSQL, MinIO, uploads, timelines, metrics and exports.
- Analysis library tests cover short two-impact candidates, missing/noisy audio, duplicate onsets and continuous rallies.
- Browser acceptance on IMG_8316.MOV: generation, manual visual boundary confirmation, pause-before-answer, submission, continuation, and persisted history after refresh.

# Pingpong3D integration deployment

The SpinRead API, PostgreSQL metadata, MinIO media, authentication and coach consent remain authoritative. This release adds per-stroke replay, separate model/confirmed placement statistics, NLF41 court-coordinate replay with fitted ball segments, and the quiz → coach reference → training task connection. No separate Node/SQLite user system is introduced.

## Install the compatible revisions

Deploy the `codex/pingpong3d-integration` branches of `ccjeff/spinread-server` and `ccjeff/spinread-web` together. The analysis library is pinned in `requirements-analysis.txt`; for editable development place the three SpinRead repositories beside one another and install `../pingpong-training` instead.

```bash
python -m pip install -e '.[test]'
python -m pip install -r requirements-analysis.txt
python -m alembic upgrade head
```

Take the deployment's normal database backup before migration. `0008_replay_reviews` and `0009_replay_training_bridge` are additive upgrades. They do not delete or regenerate videos, active timelines, quiz attempts, plans or retest snapshots. Do not run a downgrade as a rollback: it removes the newly added records. Roll back application code while retaining the additive schema instead. Existing videos require no upload or data backfill.

The API/worker must run the same server and analysis-library revisions. Build the frontend with `npm ci && npm run build`. The dev proxy defaults to the existing API on port 8000; `SPINREAD_API_PROXY` can select an isolated development API.

## Local analysis without an NLF GPU

Install `pingpong-training[vision,pose]` in the worker environment, using the pinned checkout. Set absolute paths to the separately obtained checkpoints:

```dotenv
SPINREAD_BLURBALL_WEIGHTS=/models/blurball_best
SPINREAD_POSE_WEIGHTS=/models/pose_landmarker_full.task
SPINREAD_POSE_FPS=12
```

The existing upload pipeline produces optional 2D ball detections and MediaPipe hip-relative 3D estimates. For older videos, choose **补充／更新复盘分析** in the replay panel. This queues `REPLAY_ANALYSIS`, retaining the active timeline and all existing training records. It differs from **重建时间线**, the existing full pipeline operation.

The worker stores a new immutable evidence object. A failed run leaves the previous successful replay usable. A modality with no detections does not erase earlier usable evidence; combined results retain per-modality provenance. A timeline edit during a job marks it stale instead of attaching it to the new timeline. No detections means unavailable, not a successful pose measurement.

## Optional Pingpong3D worker

Install the operator-authorized `Pingpong3D/pingpong-analyzer-backend` checkout and its sibling `pingpong-biomechanics-backend` in a separate environment. The adapter was checked against analyzer revision `9c4ff43e904125fcbc1674c1f20b90601afa16e0`. Follow that project's model and local NVIDIA GPU setup; verify its runtime before enabling the bridge. The private source, SMPL assets, weights and recordings are not redistributed in these SpinRead repositories. Their licenses must permit the intended deployment.

```dotenv
SPINREAD_PINGPONG3D_REPO=/opt/Pingpong3D/pingpong-analyzer-backend
SPINREAD_PINGPONG3D_PYTHON=/opt/pingpong-python/bin/python
SPINREAD_PINGPONG3D_MODEL_DIR=/opt/Pingpong3D/models
SPINREAD_PINGPONG3D_TIMEOUT_SECONDS=7200
SPINREAD_PINGPONG3D_MAX_RALLIES=80
```

Upstream runtime variables such as `MATCH_GPU_PYTHON`, `PP_GPU_HOME`, `MATCH_TABLE_PYTHON`, `MATCH_ANNY_PYTHON`, `NLF_FT_MODEL`, `NLF_API_BASE` and `NLF_API_TOKEN` must be exported into the worker process if needed; they are not SpinRead-prefixed settings. SpinRead storage/JWT credentials are not passed to the upstream process. The bridge selects local GPU execution and does not pass Runpod provisioning credentials.

When the owner selects Pingpong3D, both players' handedness and distinct shirt colors must be entered explicitly. Optional height is entered in centimetres and converted to upstream metres; mass is in kilograms. Missing anthropometrics use upstream defaults, so body-dependent force cues remain estimates for coach review.

The bridge ingests a scratch copy of the original, scans the video, selects upstream rallies that overlap current SpinRead rallies, then runs reconstruction and analysis. Source time is preserved. It loads aggregate or per-rally viewer JSON, tactics and biomechanics through the analysis adapter. Contacts are associated with surviving SpinRead hits only when mutually unique within 120 ms; ambiguous contacts remain excluded with a limitation message. The original timeline is never rewritten.

All upstream outputs and its reference-library database are isolated inside the job scratch directory, which is cleaned after completion. This release does not ship a professional-video reference corpus; the upstream similarity library is empty unless a future explicitly configured corpus is provided. It does not import or publish professional footage.

Missing GPU, missing models, required table calibration, timeout and unusable evidence are explicit failures. Fixed-camera upstream recordings may produce placements and body reconstruction without 3D ball trajectories; such runs are partially ready. Current SpinRead 3D renders the reconstructed skeleton and available fitted ball segments; SMPL surface meshes are not loaded or redistributed. Biomechanics cues are model estimates, not measured muscle activation, and preserve upstream quality gates.

## API and review workflow

- `GET /api/videos/{video}/replay-analysis?timeline_version=V` reports status and configured engines.
- `POST /api/videos/{video}/replay-analysis` with `Idempotency-Key` and `{timeline_version,engine,players?}` queues owner-only enrichment. Authorized coaches can inspect results but cannot start jobs or alter source calibration.
- Replay responses remain bounded to 30 seconds; every new modality is clipped to the requested interval.
- Automatic placements and confirmed placements have separate denominators; unknown or partial positions are excluded from complete grids.
- Generate replay quiz drafts, review their boundaries and approve them. Evidence and reference answers are revealed in practice only after an attempt of that exact quiz revision.
- A coach reference is immutable, versioned and bound to an active hit and evidence artifact. Training tasks retain its id. Existing consent revocation, coach locks and retest comparability rules still apply.

See [API contracts](replay-training-bridge.md) and the analysis repository's `docs/pingpong3d-adapter.md` for data conventions.

## Validation for this integration

- Analysis library: 106 passed; 2 optional tests skipped.
- Server: 101 isolated tests passed; 24 live PostgreSQL/MinIO tests deliberately excluded from this run to avoid modifying the user's running store.
- Additive migration test preserves legacy video, quiz attempt, plan and retest rows.
- Frontend: build, lint and 16 tests; browser flow covers replay, camera rotation, model/confirmed grids, candidate confirmation, draft generation, post-attempt reveal, coach reference and linked locked task creation.
- Real 8-second local video: 239 sampled ball frames (74 observed detections), 96 pose frames (92 with people), no inference failures after macOS graphics access was available. These are execution checks, not accuracy benchmarks.
- NLF/SMPL GPU inference has not been executed on this Mac. Upstream conversion, runner orchestration and API/UI integration use synthetic, source-format fixtures; an operator-provisioned upstream GPU/model runtime remains required for that engine.

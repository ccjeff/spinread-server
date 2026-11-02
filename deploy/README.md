# Local image and dispatcher verification

No command in this document pushes an image or deploys a cloud resource. Run from the `spinread-server` repository. Keep the sibling `pingpong-training` checkout available.

## Build

Prepare a **new** directory using the source allowlist. Do not pass the repository root or the user's video library directly as Docker build context.

```sh
python deploy/prepare_context.py --output /tmp/rallyloop-build-review
docker build --platform linux/amd64 --target api -t rallyloop-local:api-20261101 /tmp/rallyloop-build-review
docker build --platform linux/amd64 --target dispatcher -t rallyloop-local:dispatcher-20261101 /tmp/rallyloop-build-review
docker build --platform linux/amd64 --target compute -t rallyloop-local:compute-20261101 /tmp/rallyloop-build-review
docker build --platform linux/amd64 --target verify -t rallyloop-local:verify-20261101 /tmp/rallyloop-build-review
```

The optional `gpu` target installs the locked CUDA/Torch runtime. It contains no weights or Pingpong3D private upstream checkout and is not yet GPU-accepted. Model manifests hash both their exact JSON bytes and each referenced model file; engines remain disabled until a separately reviewed acceptance run.

## Isolated checks

```sh
PYTHONPATH=.:../pingpong-training/src python -m pytest deploy/tests/test_remote.py
docker run --rm --network none --memory 512m rallyloop-local:dispatcher-20261101 /app/bin/rallyloop-dispatcher --check
docker compose -p rallyloop-verify-20261101 -f deploy/compose.verify.yaml up --abort-on-container-exit --exit-code-from verify
docker compose -p rallyloop-verify-20261101 -f deploy/compose.verify.yaml down
```

The compose file creates a new PostgreSQL database on tmpfs and exposes no host ports. Tests refuse a non-empty DB or a DB other than `pg/rallyloop_verify`. The migration test saves all pre-existing table rows before 0010 and checks they are identical afterward, including a video and quiz attempt. No host video directories or credentials are mounted.

Do not run the repository's entire legacy live-stack test suite against the user's library. Some older queue tests mutate their configured job table. The new `deploy/tests` suite has its own fixtures and is independent of `tests/conftest.py`.

## Runtime

- API: `uvicorn spinread.api.main:app --host 0.0.0.0 --port 8000 --workers 1`.
- Dispatcher: `/app/bin/rallyloop-dispatcher`; `--check` is an offline import check, `--once` claims at most one task.
- Set `SPINREAD_RUNTIME_ENVIRONMENT=staging`, `SPINREAD_EMBED_WORKER=false`, `SPINREAD_BOOTSTRAP_STORAGE=false`, `SPINREAD_SEED_DEMO=false`, `SPINREAD_REMOTE_PROVIDER=modal` in cloud.
- Cloud requires TLS DB, HTTPS storage, non-default storage credentials, a JWT secret of at least 32 characters and exact HTTPS CORS origins. No automatic migrations or seeding.
- Only dispatcher receives `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`. The compute functions receive no DB/JWT/bucket credentials.
- `SPINREAD_REMOTE_REPLAY_ENGINES=[]`, `SPINREAD_REMOTE_VISION_ENABLED=false` are the initial settings. Enabling a model requires `SPINREAD_REMOTE_MODEL_REVISION` bound to the exact model manifest.
- Default result bundle maximum: 2GiB; runtime maximum configurable to 4GiB only after scratch/transport cost review. Larger output fails rather than silently truncating a recording.
- Existing local-worker behavior remains the default outside cloud. `remote_provider=local` is an explicitly isolated test adapter and is rejected in staging/production.

## Recovery

`remote_executions` records PREPARED → SUBMITTING → RUNNING/RESULT_PENDING → SUCCEEDED, or FAILED/TIMED_OUT/CANCELLED. A crash after submission can leave SUBMITTING without a call ID. The dispatcher reconciles the same output key until the deadline; it does not resubmit paid work. Poll transport failures and SIGTERM release the queue claim for later polling without increasing attempts.

Inspect the receipt and provider before a manual retry. Never clear a receipt or reset SUBMITTING to PREPARED merely to make progress. A newly requested job is a new possible charge. On cancellation failure, inspect the provider; platform timeout remains the backstop.

The final deployment and cost checklist is in [RallyLoopInfra/DEPLOYMENT-REVIEW.md](https://github.com/RallyLoop/RallyLoopInfra/blob/main/DEPLOYMENT-REVIEW.md). Its [GitHub delivery workflow](https://github.com/RallyLoop/RallyLoopInfra/blob/main/CI-CD.md) plans the infrastructure on main pushes and requires a manual saved-plan apply. It does not publish these application images or migrate existing videos/training data.

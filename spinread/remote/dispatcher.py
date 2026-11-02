"""Serial remote dispatcher. No GPU/FFmpeg pipeline execution on Fly."""
from __future__ import annotations

import argparse
from datetime import timedelta
import logging
from pathlib import Path
import signal
import threading
import tempfile
import uuid

from sqlalchemy import event

from spinread.config import get_settings
from spinread.core import queue
from spinread.core.db import make_engine, make_session_factory
from spinread.core.models import utcnow
from spinread.core.storage import S3ObjectStore
from spinread.remote.client import RemoteClient, before_commit, lease
from spinread.remote.protocol import RemoteError
from spinread.remote.provider import RemotePending, LeaseLost
from spinread.worker.heartbeat import keep_claim_alive

log = logging.getLogger(__name__)
# Chapter coaching remains a bounded control-plane model call (no local GPU).
# Its video frame preparation is explicit in the readiness/cost review.
KINDS = ["PIPELINE_STAGE", "FINALIZE_UPLOAD", "CLIP_RENDER", "REPLAY_ANALYSIS", "QUIZ_GENERATE", "CHAPTER_ANALYSIS"]


def run(settings=None, *, once=False, stopped=None):
    from spinread.worker.main import HANDLERS
    settings = settings or get_settings()
    if settings.remote_provider not in {"modal", "local"}:
        raise ValueError("dispatcher requires an explicit remote provider")
    stopped = stopped or threading.Event()
    Path(settings.tmp_dir).mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.db_url)
    factory = make_session_factory(engine)
    store = S3ObjectStore(settings)
    remote = RemoteClient(settings, store, stopping=stopped.is_set)
    try:
        while not stopped.is_set():
            with factory() as db:
                job = queue.claim(db, "dispatch-" + uuid.uuid4().hex, kinds=KINDS)
                if job is None:
                    if once:
                        return
                    stopped.wait(2)
                    continue
                db.info.update(dispatcher_job=job, remote_client=remote)
                event.listen(db, "before_commit", before_commit)
                try:
                    with keep_claim_alive(factory, job.id, job.claimed_by), tempfile.TemporaryDirectory(
                            prefix="dispatch-", dir=settings.tmp_dir) as scratch:
                        job_settings = settings.model_copy(update={"tmp_dir": scratch})
                        outcome = HANDLERS[job.kind](db, job_settings, store, job)
                    if outcome == "retry":
                        queue.fail(db, job.id, "RETRYABLE_STAGE_FAILURE", retryable=True)
                    else:
                        queue.complete(db, job.id)
                except LeaseLost:
                    db.rollback()
                    log.warning("Job %s lease lost; no publication", job.id)
                except RemotePending as exc:
                    db.rollback()
                    try:
                        current = lease(db, job, lock=True)
                        from spinread.core.models import Job
                        row = db.get(Job, current.id)
                        row.run_at = utcnow() + timedelta(seconds=10)
                        row.claimed_by = row.claimed_at = None
                        row.last_error = str(exc)
                        db.commit()
                    except LeaseLost:
                        db.rollback()
                except Exception as exc:
                    db.rollback()
                    try:
                        queue.fail(db, job.id, str(exc) if isinstance(exc, RemoteError) else "DISPATCH_FAILED",
                            retryable=not isinstance(exc, RemoteError))
                    except LeaseLost:
                        db.rollback()
                    # Never include SDK exceptions/URLs/credentials in logs.
                    log.warning("Job %s failed (%s)", job.id, type(exc).__name__)
                finally:
                    event.remove(db, "before_commit", before_commit)
            if once:
                return
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="offline import/entrypoint check; no settings or network")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.check:
        from spinread.worker.main import HANDLERS
        assert all(kind in HANDLERS for kind in KINDS)
        print("Dispatcher imports and handlers OK; no network or resource creation")
        return
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    logging.basicConfig(level=logging.INFO)
    run(once=args.once, stopped=stopped)


if __name__ == "__main__":
    main()

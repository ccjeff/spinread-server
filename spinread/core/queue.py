"""Postgres-backed job queue with SKIP LOCKED claiming (LLD §5.3)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from spinread.core.ids import new_id
from spinread.core.models import Job, utcnow

CLAIM_STALE_AFTER = "10 minutes"
MAX_BACKOFF = "30 minutes"
BACKOFF_BASE_SECONDS = 30


def enqueue(
    session: Session,
    kind: str,
    payload: dict,
    *,
    run_at: datetime | None = None,
    priority: int = 100,
    max_attempts: int = 5,
) -> Job:
    job = Job(
        kind=kind,
        payload=payload,
        run_at=run_at or utcnow(),
        priority=priority,
        max_attempts=max_attempts,
    )
    session.add(job)
    session.flush()
    return job


def claim(session: Session, worker_id: str) -> Job | None:
    """Atomically claim one due job. Uses SELECT ... FOR UPDATE SKIP LOCKED."""
    sql = text(
        """
        UPDATE jobs
        SET claimed_by = :worker_id, claimed_at = now()
        WHERE id = (
            SELECT id FROM jobs
            WHERE done_at IS NULL
              AND run_at <= now()
              AND (claimed_at IS NULL OR claimed_at < now() - CAST(:stale AS interval))
            ORDER BY priority, run_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id
        """
    )
    row = session.execute(
        sql, {"worker_id": worker_id, "stale": CLAIM_STALE_AFTER}
    ).first()
    if row is None:
        session.commit()  # release the transaction snapshot
        return None
    job = session.get(Job, row[0], populate_existing=True)
    session.commit()
    if job is not None:
        session.expunge(job)
    return job


def complete(session: Session, job_id: str) -> None:
    job = session.get(Job, job_id)
    if job is None:
        return
    job.done_at = utcnow()
    session.commit()


def fail(session: Session, job_id: str, error: str, *, retryable: bool) -> str:
    """Record failure. Returns 'retried' or 'dead'."""
    job = session.get(Job, job_id)
    if job is None:
        return "dead"
    job.attempts += 1
    job.last_error = error[:2000]
    if retryable and job.attempts < job.max_attempts:
        backoff_s = min(
            BACKOFF_BASE_SECONDS * (2 ** job.attempts), 30 * 60
        )
        job.run_at = datetime.now(timezone.utc).replace(microsecond=0) + _seconds(backoff_s)
        job.claimed_by = None
        job.claimed_at = None
        session.commit()
        return "retried"
    job.done_at = utcnow()
    session.commit()
    return "dead"


def _seconds(s: float):
    from datetime import timedelta

    return timedelta(seconds=s)

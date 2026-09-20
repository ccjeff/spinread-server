"""Queue semantics against real Postgres: enqueue/claim/fail-backoff/complete."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import delete

from spinread.core import queue
from spinread.core.db import make_engine, make_session_factory
from spinread.core.models import Job

pytestmark = pytest.mark.usefixtures("require_services")


@pytest.fixture()
def db(require_services):
    factory = make_session_factory(make_engine())
    session = factory()
    session.execute(delete(Job))
    session.commit()
    yield session
    session.execute(delete(Job))
    session.commit()
    session.close()


def test_enqueue_and_claim(db):
    job = queue.enqueue(db, "CLEANUP", {"x": 1})
    db.commit()
    assert job.id.startswith("job_")

    claimed = queue.claim(db, "w1")
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.claimed_by == "w1"

    # Already claimed -> nothing else claimable.
    assert queue.claim(db, "w2") is None


def test_claim_skip_locked_concurrent(db):
    for i in range(3):
        queue.enqueue(db, "CLEANUP", {"i": i})
    db.commit()

    from spinread.core.db import make_engine, make_session_factory

    other_factory = make_session_factory(make_engine())
    won: list[str] = []

    def worker(wid: str):
        s = other_factory()
        j = queue.claim(s, wid)
        if j is not None:
            won.append(j.id)
        s.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(won)) == len(won)  # SKIP LOCKED: no job claimed twice


def test_fail_retryable_backoff(db):
    job = queue.enqueue(db, "CLEANUP", {"x": 1}, max_attempts=3)
    db.commit()

    claimed = queue.claim(db, "w1")
    result = queue.fail(db, claimed.id, "boom", retryable=True)
    assert result == "retried"

    db.expire_all()
    j = db.get(Job, job.id)
    assert j.attempts == 1
    assert j.claimed_by is None and j.claimed_at is None
    assert j.run_at > job.run_at  # backoff pushed into the future
    assert j.done_at is None

    # Not yet due -> not claimable.
    assert queue.claim(db, "w1") is None


def test_fail_permanent_dead_letters(db):
    job = queue.enqueue(db, "CLEANUP", {"x": 1})
    db.commit()
    claimed = queue.claim(db, "w1")
    result = queue.fail(db, claimed.id, "deterministic", retryable=False)
    assert result == "dead"
    db.expire_all()
    j = db.get(Job, job.id)
    assert j.done_at is not None
    assert "deterministic" in (j.last_error or "")


def test_complete(db):
    job = queue.enqueue(db, "CLEANUP", {"x": 1})
    db.commit()
    claimed = queue.claim(db, "w1")
    queue.complete(db, claimed.id)
    db.expire_all()
    assert db.get(Job, job.id).done_at is not None
    assert queue.claim(db, "w1") is None

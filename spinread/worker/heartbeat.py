"""Keep long video/GPU jobs from being reclaimed while a worker is alive."""
from contextlib import contextmanager
import logging
from threading import Event, Thread
from sqlalchemy import update
from spinread.core.models import Job, utcnow

log = logging.getLogger(__name__)


def renew_claim(factory, job_id, worker_id):
    with factory.begin() as db:
        result = db.execute(update(Job).where(
            Job.id == job_id, Job.claimed_by == worker_id, Job.done_at.is_(None),
        ).values(claimed_at=utcnow()))
        return result.rowcount == 1


@contextmanager
def keep_claim_alive(factory, job_id, worker_id, interval_seconds=60):
    stopped = Event()

    def heartbeat():
        while not stopped.wait(interval_seconds):
            try:
                if not renew_claim(factory, job_id, worker_id):
                    return
            except Exception:
                log.exception("Could not renew job claim %s", job_id)

    thread = Thread(target=heartbeat, name="job-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=5)

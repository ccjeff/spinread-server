"""Opt-in tests for an EMPTY, disposable PostgreSQL container named pg only."""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


@pytest.fixture(scope="module")
def pg():
    url = os.environ.get("RALLYLOOP_TEST_DB_URL")
    if not url:
        pytest.skip("requires isolated PostgreSQL container")
    parsed = make_url(url)
    assert parsed.host == "pg" and parsed.database == "rallyloop_verify", "Refusing non-isolated DB"
    engine = create_engine(url)
    assert not inspect(engine).get_table_names(), "Test DB must be new and empty"
    os.environ["SPINREAD_DB_URL"] = url
    from spinread.config import get_settings
    get_settings.cache_clear()
    from alembic import command
    from alembic.config import Config
    config = Config("alembic.ini")
    command.upgrade(config, "0009_replay_training_bridge")
    from spinread.core.models import User, Video, Timeline, QuizItem, QuizAttempt
    with Session(engine) as db:
        db.add(User(id="preserved-user", email="preserved@example.invalid", password_hash="unused", display_name="sentinel")); db.flush()
        db.add(Video(id="preserved-video", owner_id="preserved-user", filename="retained.mp4", training_context={"note": "keep"})); db.flush()
        db.add(Timeline(id="preserved-timeline", video_id="preserved-video", version=1)); db.flush()
        db.add(QuizItem(id="preserved-quiz", video_id="preserved-video", timeline_id="preserved-timeline", start_ms=0, contact_ms=100, pause_ms=200, end_ms=300)); db.flush()
        db.add(QuizAttempt(id="preserved-attempt", quiz_item_id="preserved-quiz", user_id="preserved-user",
            request_id="retained", request_hash="retained", answer={"shot": "push"}, confidence=3, elapsed_ms=200)); db.commit()
    with engine.connect() as conn:
        before = {name: conn.execute(text('SELECT row_to_json(t) FROM "' + name + '" t')).scalars().all()
                  for name in inspect(engine).get_table_names() if name != "alembic_version"}
    command.upgrade(config, "head")
    with engine.connect() as conn:
        for name, rows in before.items():
            assert conn.execute(text('SELECT row_to_json(t) FROM "' + name + '" t')).scalars().all() == rows
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0010_remote_executions"
    yield engine
    engine.dispose()


def test_additive_migration_and_parallel_claims(pg):
    from spinread.core import queue
    from spinread.core.models import Job
    with Session(pg) as db:
        for i in range(12): queue.enqueue(db, "PIPELINE_STAGE", {"test_index": i})
        queue.enqueue(db, "NOT_SUPPORTED", {})
        db.commit()
    def claim(i):
        with Session(pg) as db:
            return queue.claim(db, f"parallel-{i}", kinds=["PIPELINE_STAGE"])
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = list(pool.map(claim, range(12)))
    assert len({job.id for job in jobs}) == 12
    with Session(pg) as db:
        assert queue.claim(db, "extra", kinds=["PIPELINE_STAGE"]) is None
        assert db.scalar(select(Job).where(Job.kind == "NOT_SUPPORTED")).claimed_by is None


def test_heartbeat_and_stale_owner_fencing(pg):
    from datetime import timedelta
    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker
    from spinread.core import queue
    from spinread.core.models import Job, utcnow
    from spinread.remote.client import before_commit
    from spinread.remote.provider import LeaseLost
    from spinread.worker.heartbeat import renew_claim
    factory = sessionmaker(pg)
    with factory() as db:
        job = queue.enqueue(db, "FENCE", {}); db.commit()
        old = queue.claim(db, "old", kinds=["FENCE"])
        row = db.get(Job, old.id); row.claimed_at = utcnow()-timedelta(minutes=11); db.commit()
        current = queue.claim(db, "new", kinds=["FENCE"])
        assert current.id == old.id
        assert not renew_claim(factory, old.id, "old")
        assert renew_claim(factory, old.id, "new")
        db.info["dispatcher_job"] = old
        event.listen(db, "before_commit", before_commit)
        with pytest.raises(LeaseLost): queue.complete(db, old.id)
        db.rollback()
        event.remove(db, "before_commit", before_commit)
        assert db.get(Job, old.id).done_at is None


def test_api_health_no_demo_or_bucket_bootstrap(pg, monkeypatch):
    from spinread.core.models import User
    from spinread.core.storage import S3ObjectStore
    from spinread.api import main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main.settings, "db_url", str(pg.url.render_as_string(hide_password=False)))
    monkeypatch.setattr(main.settings, "embed_worker", False)
    monkeypatch.setattr(main.settings, "seed_demo", False)
    monkeypatch.setattr(main.settings, "bootstrap_storage", False)
    def forbidden(*args, **kwargs): raise AssertionError("must not bootstrap or seed")
    monkeypatch.setattr(S3ObjectStore, "bootstrap", forbidden)
    monkeypatch.setattr(main, "seed_demo_user", forbidden)
    with TestClient(main.app) as client:
        assert client.get("/api/health").json() == {"ok": True}
    with Session(pg) as db:
        assert [u.id for u in db.scalars(select(User))] == ["preserved-user"]

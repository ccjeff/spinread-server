"""API contract tests isolated from the user's training database."""
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from spinread.api.main import app
from spinread.api.deps import get_current_user, get_db
from spinread.core.models import Base, User, Video, Timeline, TimelineActivePointer, QuizItem, QuizAttempt, QuizGeneration, Job


@pytest.fixture
def quiz_client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as db:
        db.add_all([User(id=u, email=f"{u}@test", password_hash="unused", display_name=u) for u in ("owner", "other")])
        db.add(Video(id="video", owner_id="owner", filename="test.mp4", state="READY", duration_ms=20000))
        db.flush()
        db.add(Timeline(id="timeline", video_id="video", version=1, state="PUBLISHED"))
        db.flush()
        db.add(TimelineActivePointer(video_id="video", timeline_id="timeline"))

    def database():
        with factory.begin() as db:
            yield db

    def user(request: Request):
        with factory() as db:
            return db.get(User, request.headers.get("X-Test-User", "owner"))

    old = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = user
    # Deliberately do not enter lifespan: no live services or embedded worker.
    c = TestClient(app)
    yield c, factory
    c.close()
    app.dependency_overrides = old
    engine.dispose()


BOUNDS = {"timeline_version": 1, "start_ms": 1000, "contact_ms": 2500, "pause_ms": 3300, "end_ms": 6000, "approval": "APPROVED"}
ANSWER = {"item_version": 1, "answer": {"spin": "UNKNOWN", "length": "SHORT", "receive": "PUSH", "note": "Discuss with coach"}, "confidence": 3, "elapsed_ms": 2000}


def create(c, **changes):
    r = c.post("/api/videos/video/quiz-items", json=BOUNDS | changes)
    assert r.status_code == 201, r.text
    return r.json()


def attempt(c, item, **changes):
    return c.post(f"/api/quiz-items/{item['id']}/attempts", json=ANSWER | changes, headers={"Idempotency-Key": "attempt-key-123"})


def test_practice_persists_unscored_attempt_and_retries_without_duplicates(quiz_client):
    c, factory = quiz_client
    item = create(c)
    r = attempt(c, item)
    assert r.status_code == 201, r.text
    assert r.json()["scored"] is False
    assert attempt(c, item).json()["id"] == r.json()["id"]
    assert attempt(c, item, confidence=5).status_code == 409
    assert c.get("/api/quiz-sets/recommended").json()["items"][0]["attempted"] is True
    assert c.get(f"/api/quiz-items/{item['id']}/attempts").json()[0]["answer"]["note"] == "Discuss with coach"
    with factory() as db:
        assert len(db.scalars(select(QuizAttempt)).all()) == 1


def test_revision_history_conflicts_and_stale_timeline(quiz_client):
    c, factory = quiz_client
    item = create(c)
    assert attempt(c, item).status_code == 201
    path = f"/api/quiz-items/{item['id']}/revisions"
    newer = c.post(path, json=BOUNDS | {"base_version": 1, "pause_ms": 3400})
    assert newer.status_code == 201
    assert newer.json()["version"] == 2
    assert c.post(path, json=BOUNDS | {"base_version": 1}).status_code == 409
    assert c.get(f"/api/quiz-items/{newer.json()['id']}/attempts").json()[0]["quiz_item_id"] == item["id"]
    with factory.begin() as db:
        assert db.get(QuizItem, item["id"]).pause_ms == 3300
        db.add(Timeline(id="timeline2", video_id="video", version=2, state="PUBLISHED"))
        db.flush()
        db.get(TimelineActivePointer, "video").timeline_id = "timeline2"
    assert c.get("/api/quiz-sets/recommended").json()["items"] == []
    assert c.get("/api/videos/video/quiz-items").json()["items"][0]["stale"] is True
    assert attempt(c, newer.json(), item_version=2).status_code == 409


@pytest.mark.parametrize("change", [{"pause_ms": 500}, {"end_ms": 21000}, {"start_ms": -1}, {"contact_ms": 3300}])
def test_invalid_boundaries_rejected(quiz_client, change):
    c, _ = quiz_client
    assert c.post("/api/videos/video/quiz-items", json=BOUNDS | change).status_code == 422


def test_drafts_and_withdrawn_items_cannot_be_practiced(quiz_client):
    c, _ = quiz_client
    item = create(c, approval="DRAFT")
    assert attempt(c, item).status_code == 409
    assert c.get("/api/quiz-sets/recommended").json()["items"] == []
    withdrawn = create(c, approval="WITHDRAWN")
    assert attempt(c, withdrawn).status_code == 409


def test_ownership_and_deleted_media_apply_to_every_quiz_path(quiz_client):
    c, factory = quiz_client
    item = create(c)
    h = {"X-Test-User": "other", "Idempotency-Key": "attempt-other"}
    for path in ("/api/videos/video/quiz-items", f"/api/quiz-items/{item['id']}/attempts", "/api/quiz-sets/recommended?video_id=video"):
        assert c.get(path, headers=h).status_code == 403
    assert c.post("/api/videos/video/quiz-candidates", headers=h).status_code == 403
    assert c.post("/api/videos/video/quiz-items", json=BOUNDS, headers=h).status_code == 403
    assert c.post(f"/api/quiz-items/{item['id']}/revisions", json=BOUNDS, headers=h).status_code == 403
    assert c.post(f"/api/quiz-items/{item['id']}/attempts", json=ANSWER, headers=h).status_code == 403
    assert c.get("/api/quiz-sets/recommended", headers=h).json()["items"] == []
    from spinread.core.models import utcnow
    with factory.begin() as db:
        db.get(Video, "video").deleted_at = utcnow()
    assert c.get("/api/quiz-sets/recommended").json()["items"] == []
    assert c.get(f"/api/quiz-items/{item['id']}/attempts").status_code == 404


def test_generation_idempotency_and_retry(quiz_client):
    c, factory = quiz_client
    path = "/api/videos/video/quiz-candidates"
    one = c.post(path).json()
    assert c.post(path).json()["id"] == one["id"]
    with factory.begin() as db:
        assert len(db.scalars(select(Job)).all()) == 1
        db.get(QuizGeneration, one["id"]).status = "FAILED"
    assert c.post(path).json()["status"] == "QUEUED"
    with factory() as db:
        assert len(db.scalars(select(Job)).all()) == 2


def test_bad_answer_and_missing_idempotency_key_rejected(quiz_client):
    c, _ = quiz_client
    item = create(c)
    assert attempt(c, item, answer={"spin": "MADE_UP", "length": "SHORT", "receive": "PUSH"}).status_code == 422
    assert c.post(f"/api/quiz-items/{item['id']}/attempts", json=ANSWER).status_code == 422


def test_generator_uses_all_onsets_and_replay_preserves_review(quiz_client, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from spinread.core.models import MediaAsset
    from spinread.product import quiz_generation
    c, factory = quiz_client
    g = c.post('/api/videos/video/quiz-candidates').json()
    with factory.begin() as db:
        db.add(MediaAsset(video_id='video', class_='ORIGINAL', object_key='original', content_hash='hash', byte_size=1, status='ACTIVE'))
    root = tmp_path / 'cache' / 'hash'
    root.mkdir(parents=True)
    (root / 'original').write_bytes(b'x')
    monkeypatch.setattr(quiz_generation, 'detect_rallies_for_video', lambda *a, **kw: ([], [2000, 2400, 7000, 7500]))
    settings = SimpleNamespace(tmp_dir=str(tmp_path), ffmpeg_bin='ffmpeg', ffprobe_bin='ffprobe')
    with factory() as db:
        job = db.scalar(select(Job).where(Job.kind == 'QUIZ_GENERATE'))
        quiz_generation.generate_quiz_candidates(db, settings, None, job)
    data = c.get('/api/videos/video/quiz-items').json()
    assert data['generation']['status'] == 'READY'
    assert len(data['items']) == 2
    assert all(i['approval'] == 'DRAFT' and i['provenance']['confidence'] is None for i in data['items'])
    with factory() as db:
        quiz_generation.generate_quiz_candidates(db, settings, None, job)
        assert len(db.scalars(select(QuizItem)).all()) == 2
    assert c.post('/api/videos/video/quiz-candidates').json()['id'] == g['id']


def test_generator_failure_is_visible_and_retryable(quiz_client, tmp_path):
    from types import SimpleNamespace
    from spinread.product.quiz_generation import generate_quiz_candidates
    c, factory = quiz_client
    c.post('/api/videos/video/quiz-candidates')
    with factory() as db:
        job = db.scalar(select(Job).where(Job.kind == 'QUIZ_GENERATE'))
        generate_quiz_candidates(db, SimpleNamespace(tmp_dir=str(tmp_path)), None, job)
    data = c.get('/api/videos/video/quiz-items').json()
    assert data['generation']['status'] == 'FAILED'
    assert data['generation']['error']
    assert c.post('/api/videos/video/quiz-candidates').json()['status'] == 'QUEUED'


def test_stale_new_attempt_is_rejected_and_reconfirmation_keeps_history(quiz_client):
    c, factory = quiz_client
    item = create(c)
    assert attempt(c, item).status_code == 201
    with factory.begin() as db:
        db.add(Timeline(id='timeline-next', video_id='video', version=2, state='PUBLISHED'))
        db.flush()
        db.get(TimelineActivePointer, 'video').timeline_id = 'timeline-next'
    response = c.post(f"/api/quiz-items/{item['id']}/attempts", json=ANSWER, headers={'Idempotency-Key': 'fresh-stale-key'})
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'QUIZ_VERSION_CONFLICT'
    revised = c.post(f"/api/quiz-items/{item['id']}/revisions", json=BOUNDS | {'timeline_version': 2, 'base_version': 1})
    assert revised.status_code == 201
    assert len(c.get('/api/quiz-sets/recommended').json()['items']) == 1
    assert c.get(f"/api/quiz-items/{revised.json()['id']}/attempts").json()[0]['quiz_item_id'] == item['id']

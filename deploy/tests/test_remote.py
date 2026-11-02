"""Isolated SQLite/filesystem tests. Never use the live-stack tests/conftest.py."""
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import zipfile

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from spinread.config import Settings
from spinread.core.models import Base, Job, MediaAsset, PipelineRun, RemoteExecution, StageRun, User, Video, utcnow
from spinread.core import queue
from spinread.remote.client import RemoteClient, before_commit
from spinread.remote.protocol import RemoteError, canonical, checked_url, digest, pack, unpack
from spinread.remote.provider import LeaseLost, RemotePending


class Files:
    bucket = "isolated-test-only"
    def __init__(self, root):
        self.root = root
        self.client = SimpleNamespace(generate_presigned_url=lambda *a, **k: "https://test.r2.cloudflarestorage.com/object?signed=test")
    def path(self, key):
        return self.root / hashlib.sha256(key.encode()).hexdigest()
    def put_bytes(self, key, data, content_type=""):
        self.path(key).write_bytes(data)
        return hashlib.sha256(data).hexdigest()
    def get_bytes(self, key):
        return self.path(key).read_bytes()
    def upload_file(self, source, key, content_type=""):
        shutil.copyfile(source, self.path(key))
    def download_file(self, key, dest):
        shutil.copyfile(self.path(key), dest)
    def head(self, key):
        path = self.path(key)
        return {"ContentLength": path.stat().st_size} if path.exists() else None
    def copy(self, source, dest):
        shutil.copyfile(self.path(source), self.path(dest))
    def delete(self, key):
        self.path(key).unlink()


@pytest.fixture
def isolated(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(User(id="usr-test", email="test@invalid.local", password_hash="unused", display_name="test"))
        video = Video(id="vid-test", owner_id="usr-test", filename="isolated.mp4", state="UPLOADED")
        run = PipelineRun(id="run-test", video_id=video.id, pipeline_version="test")
        job = Job(id="job-test", kind="PIPELINE_STAGE", payload={}, claimed_by="lease-test", claimed_at=utcnow())
        db.add_all([video, run, job]); db.commit(); db.expunge(job)
        store = Files(tmp_path)
        settings = Settings(_env_file=None, db_url="sqlite://", remote_provider="local", tmp_dir=str(tmp_path),
            embed_worker=False, bootstrap_storage=False, seed_demo=False, remote_poll_seconds=.01)
        yield db, job, video, run, settings, store
    engine.dispose()


def test_bundle_checks_identity_hash_paths_and_limits(tmp_path):
    source = tmp_path / "input"; source.write_bytes(b"test")
    bundle = tmp_path / "result.zip"
    identity = {"execution_id": "rex-test", "request_hash": "abc"}
    pack(bundle, identity, {"ok": True}, {"file": source}, max_bytes=4096)
    result, files = unpack(bundle, tmp_path / "out", identity, max_bytes=4096)
    assert result == {"ok": True} and files["file"].read_bytes() == b"test"
    with pytest.raises(RemoteError, match="IDENTITY"):
        unpack(bundle, tmp_path / "wrong", {}, max_bytes=4096)
    with pytest.raises(RemoteError, match="TOO_LARGE"):
        unpack(bundle, tmp_path / "large", identity, max_bytes=1)
    with zipfile.ZipFile(bundle, "a") as archive:
        archive.writestr("../../escaped", "bad")
    with pytest.raises(RemoteError, match="MEMBERS"):
        unpack(bundle, tmp_path / "unsafe", identity, max_bytes=4096)
    assert not (tmp_path.parent / "escaped").exists()


@pytest.mark.parametrize("url", ["http://localhost:9000/a", "https://169.254.169.254/x", "https://r2.cloudflarestorage.com.evil.com/a", "https://user:pass@test.r2.cloudflarestorage.com/a"])
def test_remote_rejects_unapproved_origins(url):
    with pytest.raises(RemoteError): checked_url(url)


def test_cloud_rejects_defaults():
    with pytest.raises(ValueError):
        Settings(_env_file=None, runtime_environment="staging")
    settings = Settings(_env_file=None, runtime_environment="staging", embed_worker=False,
        bootstrap_storage=False, seed_demo=False, jwt_secret="a"*40,
        db_url="postgresql+psycopg://test:fake@db.invalid/test?sslmode=require",
        s3_endpoint="https://test.r2.cloudflarestorage.com", s3_access_key="fake", s3_secret_key="fake",
        cors_origins=["https://test.pages.dev"], remote_provider="modal")
    assert settings.remote_replay_engines == []


def test_lease_fences_commit_and_allows_completion(isolated):
    db, job, video, *_ = isolated
    db.info["dispatcher_job"] = job
    event.listen(db, "before_commit", before_commit)
    queue.complete(db, job.id)
    assert db.get(Job, job.id).done_at is not None
    video.filename = "must-not-persist"
    with pytest.raises(LeaseLost): db.commit()
    db.rollback()
    assert db.get(Video, video.id).filename == "isolated.mp4"
    event.remove(db, "before_commit", before_commit)


def test_lost_lease_cannot_complete(isolated):
    db, job, *_ = isolated
    db.get(Job, job.id).claimed_by = "other-owner"; db.commit()
    db.info["dispatcher_job"] = job
    event.listen(db, "before_commit", before_commit)
    with pytest.raises(LeaseLost): queue.complete(db, job.id)
    db.rollback()
    assert db.get(Job, job.id).done_at is None
    event.remove(db, "before_commit", before_commit)


def test_submission_ambiguity_recovers_bundle_without_second_spawn(isolated, tmp_path):
    db, job, video, run, settings, store = isolated
    settings.remote_provider = "modal"
    class Provider:
        calls = 0
        def submit(self, request, **kwargs):
            self.calls += 1
            raise OSError("a secret URL that must not escape")
    provider = Provider()
    client = RemoteClient(settings, store, provider=provider)
    payload = {"operation": "stage", "video_id": video.id, "source": {"key": "source"}}
    with pytest.raises(RemotePending, match="SUBMISSION_UNCERTAIN"):
        client.run(db, job, payload, tmp_path / "result")
    row = db.scalar(select(RemoteExecution))
    assert row.state == "SUBMITTING" and row.call_id is None
    # The provider completed despite the lost spawn response. Restart reconciles
    # the single job-scoped object; no new paid call is submitted.
    path = tmp_path / "bundle.zip"
    pack(path, {"execution_id": row.id, "request_hash": row.request_hash}, {"ok": True}, {}, max_bytes=4096)
    store.upload_file(path, row.output_key)
    client._download_bounded = lambda key, path, size: store.download_file(key, str(path))
    result, _ = client.run(db, job, payload, tmp_path / "resume")
    assert result["ok"] and provider.calls == 1 and row.state == "SUCCEEDED"


def test_deadline_cancels_and_never_resubmits(isolated, tmp_path):
    db, job, video, run, settings, store = isolated
    settings.remote_provider = "modal"
    payload = {"operation": "stage", "video_id": video.id, "source": {"key": "source"}}
    row = RemoteExecution(job_id=job.id, operation="stage", request_hash=digest(payload), state="RUNNING",
        provider="modal", call_id="fc-test", output_key="output", deadline_at=utcnow()-timedelta(seconds=1))
    db.add(row); db.commit()
    calls = []
    provider = SimpleNamespace(cancel=lambda call: calls.append(call))
    client = RemoteClient(settings, store, provider=provider)
    with pytest.raises(RemoteError, match="DEADLINE"):
        client.run(db, job, payload, tmp_path / "result")
    assert calls == ["fc-test"] and row.state == "TIMED_OUT"
    with pytest.raises(RemoteError, match="DEADLINE"):
        client.run(db, job, payload, tmp_path / "retry")
    assert calls == ["fc-test"]


def test_put_finishes_between_head_and_poll(isolated, tmp_path):
    db, job, video, run, settings, store = isolated
    settings.remote_provider = "modal"
    payload = {"operation": "stage", "video_id": video.id, "source": {"key": "source"}}
    row = RemoteExecution(job_id=job.id, operation="stage", request_hash=digest(payload), state="RUNNING",
        provider="modal", call_id="fc-race", output_key="race-output", deadline_at=utcnow()+timedelta(minutes=5))
    db.add(row); db.commit()
    def poll(call_id):
        assert call_id == "fc-race"
        bundle = tmp_path / "race.zip"
        pack(bundle, {"execution_id": row.id, "request_hash": row.request_hash}, {"ok": True}, {}, max_bytes=4096)
        store.upload_file(bundle, row.output_key)
        return {"status": "SUCCEEDED"}
    client = RemoteClient(settings, store, provider=SimpleNamespace(poll=poll))
    client._download_bounded = lambda key, path, size: store.download_file(key, str(path))
    result, _ = client.run(db, job, payload, tmp_path / "result")
    assert result["ok"] and row.state == "SUCCEEDED"


def test_real_ffmpeg_pipeline_roundtrip_and_cache(isolated, tmp_path):
    from spinread.worker.main import _handle_pipeline_stage
    from spinread.pipeline.stage import execute_stage
    from spinread.pipeline.stages import REGISTRY
    from spinread.remote.bridge import RemoteStage
    from spinread.core.storage import file_sha256
    db, job, video, run, settings, store = isolated
    path = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=16000:duration=12", "-c:v", "libx264", "-threads", "1",
        "-c:a", "aac", "-y", str(path)], check=True, capture_output=True)
    store.upload_file(path, "original")
    asset = MediaAsset(video_id=video.id, class_="ORIGINAL", media_version=0, object_key="original",
        content_hash=file_sha256(str(path)), byte_size=path.stat().st_size, status="ACTIVE")
    db.add(asset); db.commit()
    original_bytes = store.get_bytes("original")
    client = RemoteClient(settings, store)
    # Real existing algorithms, remoted through an isolated executor and result
    # bundle; local metadata stages still use the existing publication logic.
    from spinread.remote.protocol import COMPUTE_STAGES
    for name, implementation in REGISTRY.items():
        if name == "REPORT":  # Report includes model-free report/quiz publication.
            pass
        stage_job = Job(kind="PIPELINE_STAGE", payload={"stage": name, "video_id": video.id, "pipeline_run_id": run.id},
            claimed_by="lease-" + name, claimed_at=utcnow())
        db.add(stage_job); db.commit(); db.expunge(stage_job)
        wrapped = RemoteStage(implementation, client, stage_job) if name in COMPUTE_STAGES else implementation
        stage_run = execute_stage(db, settings, store, run, video, wrapped)
        db.commit()
        assert stage_run.status in {"SUCCEEDED", "PARTIAL_SUCCESS", "SKIPPED_UNSUPPORTED"}, (name, stage_run.error_message)
    assert video.duration_ms == 12000
    assert store.get_bytes("original") == original_bytes
    assets = db.scalars(select(MediaAsset).where(MediaAsset.video_id == video.id)).all()
    assert {"ORIGINAL", "PROXY", "STREAM", "THUMBNAIL"}.issubset({a.class_ for a in assets})
    assert len(db.scalars(select(RemoteExecution)).all()) == len(COMPUTE_STAGES)
    assert all(a.status == "ACTIVE" for a in assets)
    count = len(db.scalars(select(StageRun)).all())
    cached = execute_stage(db, settings, store, run, video, RemoteStage(REGISTRY["PROBE"], client, job))
    db.commit()
    assert cached.status == "SUCCEEDED" and len(db.scalars(select(StageRun)).all()) == count
    # Exercise remote serve/receive candidate preparation and clip encoding too.
    from spinread.core.models import QuizGeneration, TimelineActivePointer, ExportManifest
    from spinread.product.quiz_generation import generate_quiz_candidates
    from spinread.worker.main import _handle_clip_render
    pointer = db.get(TimelineActivePointer, video.id)
    generation = QuizGeneration(video_id=video.id, timeline_id=pointer.timeline_id, detector_version="verify")
    db.add(generation); db.flush()
    quiz_job = Job(kind="QUIZ_GENERATE", payload={"generation_id": generation.id}, claimed_by="quiz-owner", claimed_at=utcnow())
    db.add(quiz_job); db.commit(); db.expunge(quiz_job)
    db.info.update(remote_client=client, dispatcher_job=quiz_job)
    assert generate_quiz_candidates(db, settings, store, quiz_job) == "done"
    assert generation.status == "READY", generation.error
    export = ExportManifest(video_id=video.id, timeline_version=1, kind="CLIP", intervals=[[0, 2000]], render_preset="verify", idempotency_hash="test-export")
    db.add(export); db.flush()
    clip_job = Job(kind="CLIP_RENDER", payload={"export_id": export.id}, claimed_by="clip-owner", claimed_at=utcnow())
    db.add(clip_job); db.commit(); db.expunge(clip_job)
    db.info["dispatcher_job"] = clip_job
    assert _handle_clip_render(db, settings, store, clip_job) == "done"
    assert export.asset_id and db.get(MediaAsset, export.asset_id).byte_size > 0
    assert store.get_bytes("original") == original_bytes


def test_model_manifest_checks_revision_and_file_hash(tmp_path, monkeypatch):
    from spinread.remote.model_manifest import verify
    weights = tmp_path / "weights.pt"; weights.write_bytes(b"model-test-only")
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(canonical({"files": {"nlf_weights": {"path": "weights.pt", "sha256": hashlib.sha256(weights.read_bytes()).hexdigest()}}}))
    revision = hashlib.sha256(manifest.read_bytes()).hexdigest()
    monkeypatch.setenv("SPINREAD_NLF_WEIGHTS", "")
    verify(revision, manifest)
    with pytest.raises(RemoteError, match="REVISION"):
        verify("wrong", manifest)
    weights.write_bytes(b"modified")
    with pytest.raises(RemoteError, match="HASH"):
        verify(revision, manifest)


def test_stage_retries_reuse_unique_row(isolated):
    from spinread.pipeline.stage import execute_stage, StageResult, RetryableStageError
    db, job, video, run, settings, store = isolated
    class Stage:
        stage, stage_version = "TEST", "1"
        count = 0
        def run(self, ctx):
            self.count += 1
            if self.count == 1: raise RetryableStageError("test transient")
            return StageResult()
    stage = Stage()
    first = execute_stage(db, settings, store, run, video, stage); db.commit()
    second = execute_stage(db, settings, store, run, video, stage, attempt=2); db.commit()
    third = execute_stage(db, settings, store, run, video, stage, attempt=3); db.commit()
    assert first.id == second.id == third.id and stage.count == 2
    assert len(db.scalars(select(StageRun)).all()) == 1

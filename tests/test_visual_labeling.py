from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import select
from test_quizzes import quiz_client
from spinread.config import Settings
from spinread.core.models import Video, Timeline, TimelineActivePointer, TimelineItem
from spinread.pipeline.vision import vision_config, vision_fingerprint
from spinread.pipeline.stages import activity_stage, rally_stage
from spinread.pipeline.stages.events_stage import EventsStage
from spinread.pipeline.stages.timeline_stage import TimelineStage
from pingpong_training.analysis.visual_events import VisualAnalysis, VisualRally, visual_activity_items, ACTIVITY_VERSION, EVENT_VERSION, RALLY_VERSION
from pingpong_training.timeline.models import TimelineArtifact, VideoRef


def test_visual_backend_identity_changes_with_weights(tmp_path):
    weights=tmp_path/'model';weights.write_bytes(b'one')
    a=Settings(blurball_weights=str(weights));before=vision_fingerprint(a)
    weights.write_bytes(b'two')
    assert before!=vision_fingerprint(a)
    assert vision_fingerprint(Settings(blurball_weights=''))=='legacy-audio'
    assert vision_config(Settings(blurball_weights='')) is None


def test_visual_stages_publish_new_hierarchy_and_preserve_history(quiz_client, tmp_path, monkeypatch):
    _,factory=quiz_client
    r=VisualRally(500,1800,(650,1300));analysis=VisualAnalysis(rallies=[r])
    artifact=TimelineArtifact(stage='ACTIVITY',stage_version=ACTIVITY_VERSION,
        video=VideoRef(path='test.mp4',duration_ms=20000),items=visual_activity_items(analysis,20000))
    seen=[]
    def activity(*a,**kw):seen.append(kw['blurball_config']);return artifact
    def rallies(*a,**kw):seen.append(kw['blurball_config']);return [r],list(r.hits_ms)
    monkeypatch.setattr(activity_stage,'analyze_video',activity)
    monkeypatch.setattr(rally_stage,'detect_rallies_for_video',rallies)
    with factory.begin() as db:
        video=db.get(Video,'video');old=db.get(TimelineActivePointer,'video').timeline_id
        payloads={}
        ctx=SimpleNamespace(session=db,video=video,settings=Settings(blurball_weights='/configured/model'),
            ensure_original_local=lambda:(Path('test.mp4'),tmp_path),prior_artifacts={},load_artifact_json=lambda k:payloads[k])
        for stage in [activity_stage.ActivityStage(),rally_stage.RallyStage(),EventsStage()]:
            result=stage.run(ctx);payloads[stage.stage]=result.artifact_json;ctx.prior_artifacts[stage.stage]=stage.stage
        TimelineStage().run(ctx)
        new=db.get(TimelineActivePointer,'video').timeline_id
        assert new!=old and db.get(Timeline,old) is not None
        rows=db.scalars(select(TimelineItem).where(TimelineItem.timeline_id==new)).all()
        rr=[row for row in rows if row.type=='RALLY'];hits=[row for row in rows if row.type=='HIT_CANDIDATE']
        assert len(rr)==1 and len(hits)==2
        assert rr[0].attributes['hit_detector']==EVENT_VERSION
        assert rr[0].provenance['source_id']==RALLY_VERSION
        assert all(h.provenance['source_id']==EVENT_VERSION for h in hits)
        assert all(h.parent_id==rr[0].id for h in hits)
        top=sorted((row for row in rows if row.parent_id is None),key=lambda row:row.start_ms)
        assert [(x.start_ms,x.end_ms,x.type) for x in top]==[(0,500,'UNKNOWN'),(500,1800,'RALLY_LIKE'),(1800,20000,'UNKNOWN')]
        assert all(c.weights=='/configured/model' for c in seen)


def test_relabel_reuses_media_and_queues_complete_analysis(quiz_client):
    import pytest
    from spinread.core.models import Artifact, MediaAsset, PipelineRun, StageRun, Job
    from spinread.product.relabel_video import create_relabel_run, REUSED_STAGES
    from spinread.product.timeline import EditError, VersionConflict
    from spinread.pipeline.orchestrator import progress
    _, factory = quiz_client
    with factory.begin() as db:
        db.add(MediaAsset(id="original", video_id="video", class_="ORIGINAL",
            object_key="original", content_hash="source", byte_size=1, status="ACTIVE"))
        old = PipelineRun(id="old-run", video_id="video", pipeline_version="1.1.0", state="SUCCEEDED")
        db.add(old); db.flush()
        for stage in REUSED_STAGES:
            art = Artifact(id="art-"+stage, video_id="video", pipeline_run_id=old.id,
                stage=stage, stage_version="old", object_key=stage, content_hash=stage)
            db.add(art)
            db.add(StageRun(pipeline_run_id=old.id, stage=stage, stage_version="old",
                idempotency_key=stage, status="SUCCEEDED", input_artifact_ids=["original"],
                output_artifact_id=art.id))
        db.flush()
        with pytest.raises(VersionConflict):
            create_relabel_run(db, "video", 99)
        run = create_relabel_run(db, "video", 1)
        rows = db.scalars(select(StageRun).where(StageRun.pipeline_run_id == run.id)).all()
        assert {r.stage for r in rows if r.status=="REUSED_CACHE"} == set(REUSED_STAGES)
        assert {r.stage for r in rows if r.status=="QUEUED"} == {"ACTIVITY"}
        assert db.get(TimelineActivePointer, "video").timeline_id == "timeline"
        assert progress(db, "video")[2] == 33
        jobs = db.scalars(select(Job)).all()
        assert len(jobs)==1 and jobs[0].payload["stage"]=="ACTIVITY"
        with pytest.raises(EditError, match="Analysis in progress"):
            create_relabel_run(db, "video", 1)


def test_long_job_renewal_respects_claim_owner_and_completion(quiz_client):
    from datetime import datetime, timezone
    from spinread.core.models import Job
    from spinread.worker.heartbeat import renew_claim
    _, factory = quiz_client
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with factory.begin() as db:
        db.add(Job(id="long-job", kind="PIPELINE_STAGE", payload={},
            claimed_by="worker-a", claimed_at=old))
    assert not renew_claim(factory, "long-job", "worker-b")
    assert renew_claim(factory, "long-job", "worker-a")
    with factory.begin() as db:
        job=db.get(Job,"long-job")
        assert job.claimed_at.year > 2020
        job.done_at = old
    assert not renew_claim(factory, "long-job", "worker-a")

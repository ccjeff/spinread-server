"""Event-driven orchestrator (LLD §5.2). Not a separate service: tick() is
invoked when a job completes, when an upload finalizes, and on retry."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.core import queue
from spinread.core.models import Job, PipelineRun, StageRun, Video, utcnow
from spinread.pipeline.dag import (
    PIPELINE_VERSION,
    SATISFIED,
    STAGE_MAX_ATTEMPTS,
    STAGE_VIDEO_STATE,
    STAGES,
    TERMINAL,
)

log = logging.getLogger(__name__)


def _latest_stage_statuses(session: Session, pipeline_run_id: str) -> dict[str, StageRun]:
    """Most recent stage_run per stage in a pipeline run."""
    rows = session.scalars(
        select(StageRun)
        .where(StageRun.pipeline_run_id == pipeline_run_id)
        .order_by(StageRun.started_at.asc().nulls_first(), StageRun.id.asc())
    ).all()
    latest: dict[str, StageRun] = {}
    for sr in rows:
        latest[sr.stage] = sr
    return latest


def tick(session: Session, pipeline_run_id: str) -> None:
    """Enqueue newly-ready stages; roll up run/video state when terminal.

    Caller owns the transaction.
    """
    run = session.get(PipelineRun, pipeline_run_id)
    if run is None or run.state != "RUNNING":
        return
    video = session.get(Video, run.video_id)
    if video is None or video.deleted_at is not None:
        return

    statuses = _latest_stage_statuses(session, run.id)

    # Any retryable failure not yet re-queued keeps the run open; the queue
    # backoff re-enters via its own PIPELINE_STAGE job, so tick must not
    # double-enqueue a stage that already has an open job.
    open_job_stages = set(
        session.scalars(
            select(Job.payload["stage"].as_string()).where(
                Job.kind == "PIPELINE_STAGE",
                Job.done_at.is_(None),
                Job.payload["pipeline_run_id"].as_string() == run.id,
            )
        ).all()
    )

    ready: list[str] = []
    for stage, needs in STAGES.items():
        if stage in statuses and statuses[stage].status in (
            TERMINAL | {"QUEUED", "RUNNING"}
        ):
            continue
        if stage in open_job_stages:
            continue
        if all(
            dep in statuses and statuses[dep].status in SATISFIED for dep in needs
        ):
            ready.append(stage)

    for stage in ready:
        log.info("pipeline %s: enqueueing stage %s", run.id, stage)
        session.add(
            StageRun(
                pipeline_run_id=run.id,
                stage=stage,
                stage_version="",
                idempotency_key=f"pending-{run.id}-{stage}",
                status="QUEUED",
                attempt=0,
                input_artifact_ids=[],
            )
        )
        queue.enqueue(
            session,
            "PIPELINE_STAGE",
            {"pipeline_run_id": run.id, "video_id": video.id, "stage": stage},
            max_attempts=STAGE_MAX_ATTEMPTS.get(stage, 5),
        )
        video.state = STAGE_VIDEO_STATE.get(stage, video.state)

    if ready:
        return

    # Nothing new to enqueue: if every stage is terminal, roll up.
    all_statuses = [statuses[s].status for s in STAGES if s in statuses]
    if len(all_statuses) < len(STAGES):
        return  # some stage neither terminal nor queued yet (retry in flight)
    if not all(s in TERMINAL for s in all_statuses):
        return

    run.finished_at = utcnow()
    n_perm_failed = sum(1 for s in all_statuses if s == "PERMANENT_FAILURE")
    n_partial = sum(
        1 for s in all_statuses if s in ("PARTIAL_SUCCESS", "SKIPPED_UNSUPPORTED")
    )
    if n_perm_failed:
        run.state = "FAILED"
        video.state = "PERMANENT_FAILURE"
    elif n_partial:
        run.state = "PARTIAL"
        video.state = "PARTIAL_READY"
    else:
        run.state = "SUCCEEDED"
        video.state = "READY"
    log.info("pipeline %s finished: %s (video %s)", run.id, run.state, video.state)


def progress(session: Session, video_id: str) -> tuple[PipelineRun | None, dict[str, StageRun], int]:
    """Latest run + per-stage status + progress_pct (terminal stages / DAG size)."""
    run = session.scalar(
        select(PipelineRun)
        .where(PipelineRun.video_id == video_id)
        .order_by(PipelineRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        return None, {}, 0
    statuses = _latest_stage_statuses(session, run.id)
    n_terminal = sum(1 for sr in statuses.values() if sr.status in TERMINAL)
    pct = round(n_terminal / len(STAGES) * 100)
    return run, statuses, pct

"""Analysis reports: read the active (latest PUBLISHED) report + findings."""

from __future__ import annotations
from spinread.core.access import viewable_video, audit

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.api.deps import get_current_user, get_db, get_owned_video
from spinread.api.errors import not_found
from spinread.core.models import AnalysisReport, Finding, User

router = APIRouter(prefix="/api/videos", tags=["reports"])


class FindingOut(BaseModel):
    id: str
    category: str
    observation: str
    evidence_intervals: list
    sample_count: int
    priority_score: float
    limitations: list
    state: str


class ReportOut(BaseModel):
    report_id: str
    video_id: str
    timeline_version: int
    state: str
    metric_versions: dict[str, Any]
    structured: dict[str, Any]
    findings: list[FindingOut]


@router.get("/{video_id}/reports/active", response_model=ReportOut)
def get_active_report(
    video_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReportOut:
    video = viewable_video(video_id, db, user)
    report = db.scalar(
        select(AnalysisReport)
        .where(AnalysisReport.video_id == video.id, AnalysisReport.state == "PUBLISHED")
        .order_by(AnalysisReport.published_at.desc())
        .limit(1)
    )
    if report is None:
        raise not_found("no published report for this video")
    findings = db.scalars(
        select(Finding)
        .where(Finding.report_id == report.id)
        .order_by(Finding.priority_score.desc())
    ).all()
    return ReportOut(
        report_id=report.id,
        video_id=report.video_id,
        timeline_version=report.timeline_version,
        state=report.state,
        metric_versions=report.metric_versions,
        structured=report.structured,
        findings=[
            FindingOut(
                id=f.id,
                category=f.category,
                observation=f.observation,
                evidence_intervals=f.evidence_intervals,
                sample_count=f.sample_count,
                priority_score=f.priority_score,
                limitations=f.limitations,
                state=f.state,
            )
            for f in findings
        ],
    )

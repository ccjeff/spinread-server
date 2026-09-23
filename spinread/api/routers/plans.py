"""Versioned training proposals, coach overrides and immutable retests."""
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from spinread.api.deps import get_current_user, get_db, get_owned_video
from spinread.api.errors import ApiError, forbidden, not_found
from spinread.core.models import (AnalysisReport, Finding, TrainingPlan, PlanItem,
    PlanItemRetest, Video, User, utcnow)
from spinread.core.access import audit, viewable_video, coach_can_view
from spinread.core.mutations import mutate, version_check
from spinread.product.report_policy import public_plan_item, public_finding
from spinread.product.compare import compare, report_current, METRICS

router = APIRouter(prefix="/api", tags=["training-plans"])
DB = Annotated[Session, Depends(get_db)]
Actor = Annotated[User, Depends(get_current_user)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=100)]


class ContextIn(BaseModel):
    base_version: int = Field(ge=1)
    practice_context: str = Field(min_length=1, max_length=120)
    target_identity: str = Field(min_length=1, max_length=120)
    camera_setup: str = Field(min_length=1, max_length=120)
    opponent_or_feeder: str = Field(min_length=1, max_length=120)


@router.get("/videos/{video_id}/context")
def get_context(video_id: str, db: DB, user: Actor):
    v = viewable_video(video_id, db, user)
    return {"version": v.context_version, "context": v.training_context}


@router.patch("/videos/{video_id}/context")
def set_context(video_id: str, body: ContextIn, db: DB, user: Actor, key: Key):
    v = get_owned_video(video_id, db, user)
    def action():
        db.refresh(v, with_for_update=True)
        version_check(v.context_version, body.base_version)
        values = {k: value.strip() for k, value in body.model_dump(exclude={"base_version"}).items()}
        if not all(values.values()):
            raise ApiError(422, "INVALID_CONTEXT", "录制场景字段不能为空")
        v.training_context, v.context_version = values, v.context_version + 1
        audit(db, user.id, "CONTEXT_UPDATED", v.id, v.id, version=v.context_version)
        return {"version": v.context_version, "context": v.training_context}
    return mutate(db, user, key, f"context:{video_id}", body.model_dump(), action)


def item_out(db, item):
    report = db.get(AnalysisReport, item.source_report_id)
    return {k: getattr(item, k) for k in ("id", "plan_id", "version", "priority", "title", "source_report_id",
        "finding_ids", "drill", "retest", "source", "locked_by_coach", "status", "player_note", "history")} | {
        "video_id": report.video_id, "baseline_stale": not report_current(db, report)}


def plan_out(db, plan, user):
    if plan is None: return {"plan": None}
    items = db.scalars(select(PlanItem).where(PlanItem.plan_id == plan.id).order_by(PlanItem.priority, PlanItem.created_at)).all()
    visible = []
    for item in items:
        if not public_plan_item(db, item): continue
        report = db.get(AnalysisReport, item.source_report_id)
        video = db.get(Video, report.video_id)
        if video.deleted_at is None and (video.owner_id == user.id or coach_can_view(db, user, video)):
            visible.append(item_out(db, item))
    return {"plan": {"id": plan.id, "user_id": plan.user_id, "state": plan.state, "items": visible}}


@router.get("/users/{user_id}/training-plans/active")
def active_plan(user_id: str, db: DB, user: Actor):
    if user_id != user.id and user.role != "COACH": raise forbidden()
    plan = db.scalar(select(TrainingPlan).where(TrainingPlan.user_id == user_id, TrainingPlan.state == "ACTIVE"))
    result = plan_out(db, plan, user)
    if user_id != user.id:
        videos = db.scalars(select(Video).where(Video.owner_id == user_id, Video.deleted_at.is_(None)))
        if not any(coach_can_view(db, user, v) for v in videos):
            raise forbidden("没有获准访问的训练计划")
    return result


class Drill(BaseModel):
    description: str = Field(min_length=1, max_length=3000)
    target_repetitions: int = Field(default=10, ge=1, le=10000)
    sessions: int = Field(default=1, ge=1, le=100)


class RetestCriteria(BaseModel):
    context: str = Field(min_length=1, max_length=120)
    metric: Literal["active_fraction", "confidence_coverage", "rally_duration_ms.mean", "rally_duration_ms.max", "hits_per_rally.mean"]
    minimum_samples: int = Field(default=10, ge=4, le=10000)
    direction: Literal["increase", "decrease"] = "increase"
    relative_threshold: float = Field(default=.2, ge=0, le=5)


class GenerateIn(BaseModel):
    report_id: str


# Only validated training findings may be mapped to prescriptions.
# Current rules are detector diagnostics, so no automated prescriptions yet.
TEMPLATES = {}


def ensure_plan(db, user_id, report_id):
    plan = db.scalar(select(TrainingPlan).where(TrainingPlan.user_id == user_id, TrainingPlan.state == "ACTIVE"))
    if plan is None:
        plan = TrainingPlan(user_id=user_id, source_report_id=report_id)
        db.add(plan); db.flush()
    return plan


@router.post("/training-plans/generate")
def generate(body: GenerateIn, db: DB, user: Actor, key: Key):
    report = db.get(AnalysisReport, body.report_id)
    if report is None: raise not_found("报告不存在")
    video = get_owned_video(report.video_id, db, user)
    def action():
        if not report_current(db, report): raise ApiError(409, "REPORT_STALE", "请等待当前时间线报告生成")
        plan = ensure_plan(db, user.id, report.id)
        existing = db.scalars(select(PlanItem).where(PlanItem.plan_id == plan.id)).all()
        seen = {f for item in existing for f in item.finding_ids}
        findings = db.scalars(select(Finding).where(Finding.report_id == report.id,
            Finding.state == "PUBLISHED", Finding.sample_count >= 4).order_by(Finding.priority_score.desc())).all()
        added = 0
        for f in findings:
            if f.id in seen or f.category not in TEMPLATES or added >= 3: continue
            title, description, metric, direction = TEMPLATES[f.category]
            item = PlanItem(plan_id=plan.id, source_report_id=report.id, priority=added+1,
                title=title, finding_ids=[f.id], drill=Drill(description=description).model_dump(),
                retest=RetestCriteria(context=video.training_context.get("practice_context") or "待确认训练场景",
                    metric=metric, direction=direction).model_dump())
            db.add(item); added += 1
        db.flush()
        audit(db, user.id, "PLAN_PROPOSED", plan.id, video.id, added=added, report_id=report.id)
        return plan_out(db, plan, user) | {"added": added}
    return mutate(db, user, key, "generate-plan", body.model_dump(), action)


class NewItem(GenerateIn):
    priority: int = Field(default=1, ge=1, le=100)
    title: str = Field(min_length=1, max_length=200)
    drill: Drill
    retest: RetestCriteria


@router.post("/training-plans/items")
def new_item(body: NewItem, db: DB, user: Actor, key: Key):
    report = db.get(AnalysisReport, body.report_id)
    if report is None: raise not_found("报告不存在")
    video = viewable_video(report.video_id, db, user)
    def action():
        db.scalar(select(User).where(User.id == video.owner_id).with_for_update())
        if user.id != video.owner_id and not coach_can_view(db, user, video, lock=True): raise forbidden()
        if not report_current(db, report): raise ApiError(409, "REPORT_STALE", "请使用当前报告")
        plan = ensure_plan(db, video.owner_id, report.id)
        item = PlanItem(plan_id=plan.id, source_report_id=report.id, title=body.title, priority=body.priority,
            drill=body.drill.model_dump(), retest=body.retest.model_dump(), source="USER" if user.id == video.owner_id else "COACH", locked_by_coach=user.id != video.owner_id)
        db.add(item); db.flush()
        audit(db, user.id, "PLAN_ITEM_CREATED", item.id, report.video_id)
        return item_out(db, item)
    return mutate(db, user, key, "new-plan-item", body.model_dump(), action)


def owned_item(db, user, plan_id, item_id):
    item = db.get(PlanItem, item_id)
    if item is None or item.plan_id != plan_id: raise not_found("训练任务不存在")
    report = db.get(AnalysisReport, item.source_report_id)
    video = viewable_video(report.video_id, db, user)
    return item, report, video


class EditItem(BaseModel):
    base_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    priority: int | None = Field(default=None, ge=1, le=100)
    drill: Drill | None = None
    retest: RetestCriteria | None = None
    status: Literal["ACTIVE", "DONE", "DROPPED"] | None = None
    player_note: str | None = Field(default=None, max_length=4000)
    locked_by_coach: bool | None = None


@router.patch("/training-plans/{plan_id}/items/{item_id}")
def edit_item(plan_id: str, item_id: str, body: EditItem, db: DB, user: Actor, key: Key):
    item, report, video = owned_item(db, user, plan_id, item_id)
    def action():
        if user.id != video.owner_id:
            if not coach_can_view(db, user, video, lock=True): raise forbidden()
        db.refresh(item, with_for_update=True)
        version_check(item.version, body.base_version)
        changes = body.model_dump(exclude={"base_version"}, exclude_none=True)
        if user.id == video.owner_id:
            if "locked_by_coach" in changes or (item.locked_by_coach and set(changes) - {"status", "player_note"}):
                raise forbidden("教练锁定的训练内容需要教练修改；仍可记录进度和感受")
        elif "player_note" in changes:
            raise forbidden("球员反馈由球员本人填写")
        previous = {k: getattr(item, k) for k in ("version", "title", "priority", "drill", "retest", "status", "source", "locked_by_coach", "player_note")}
        item.history = [*item.history, previous | {"actor_id": user.id, "changed_at": utcnow().isoformat()}]
        for name, value in changes.items(): setattr(item, name, value)
        if set(changes) - {"status", "player_note"}: item.source = "USER" if user.id == video.owner_id else "COACH"
        item.version, item.updated_at = item.version + 1, utcnow()
        audit(db, user.id, "PLAN_OVERRIDE", item.id, video.id, version=item.version, fields=list(changes))
        db.flush()
        return item_out(db, item)
    return mutate(db, user, key, f"edit-plan-item:{item.id}", body.model_dump(), action)


class RetestIn(BaseModel):
    video_id: str
    base_version: int = Field(ge=1)


def retest_out(row):
    return {"id": row.id, "video_id": row.video_id, "plan_item_version": row.plan_item_version,
        "result": row.result, "created_at": row.created_at}


@router.post("/training-plans/{plan_id}/items/{item_id}/retests")
def retest(plan_id: str, item_id: str, body: RetestIn, db: DB, user: Actor, key: Key):
    item, baseline, source = owned_item(db, user, plan_id, item_id)
    if user.id != source.owner_id: raise forbidden("由球员选择并提交复测视频")
    target = get_owned_video(body.video_id, db, user)
    def action():
        db.refresh(item, with_for_update=True)
        version_check(item.version, body.base_version)
        report = db.scalar(select(AnalysisReport).where(AnalysisReport.video_id == target.id,
            AnalysisReport.state == "PUBLISHED").order_by(AnalysisReport.published_at.desc()).limit(1))
        if report is None: raise ApiError(409, "REPORT_NOT_READY", "复测视频报告尚未生成")
        result = compare(db, baseline, report, item.retest)
        row = PlanItemRetest(plan_item_id=item.id, plan_item_version=item.version, video_id=target.id,
            report_id=report.id, result=result)
        db.add(row); db.flush()
        audit(db, user.id, "RETEST_RECORDED", row.id, target.id, plan_item_id=item.id)
        return retest_out(row)
    return mutate(db, user, key, f"retest:{item.id}", body.model_dump(), action)


@router.get("/training-plans/{plan_id}/items/{item_id}/retests")
def retests(plan_id: str, item_id: str, db: DB, user: Actor):
    item, _, _ = owned_item(db, user, plan_id, item_id)
    rows = db.scalars(select(PlanItemRetest).where(PlanItemRetest.plan_item_id == item.id).order_by(PlanItemRetest.created_at.desc()))
    result = []
    for row in rows:
        v = db.get(Video, row.video_id)
        if v.deleted_at is None and (user.id == v.owner_id or coach_can_view(db, user, v)):
            result.append(retest_out(row))
    return {"items": result}


@router.get("/reports/{report_id}/evidence")
def evidence(report_id: str, db: DB, user: Actor):
    report = db.get(AnalysisReport, report_id)
    if report is None: raise not_found("报告不存在")
    viewable_video(report.video_id, db, user)
    rows = db.scalars(select(Finding).where(Finding.report_id == report.id))
    return {"report_id": report.id, "video_id": report.video_id, "timeline_version": report.timeline_version,
        "findings": [{"id": f.id, "observation": f.observation, "intervals": f.evidence_intervals,
            "sample_count": f.sample_count, "limitations": f.limitations, "state": f.state} for f in rows if public_finding(f)]}

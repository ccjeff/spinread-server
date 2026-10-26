"""Player-controlled coach relationships, per-video access and review history."""
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from spinread.api.deps import get_current_user, get_db, get_owned_video
from spinread.api.errors import ApiError, forbidden, not_found
from spinread.core.models import (User, Video, CoachGrant, ConsentGrant, ReviewRequest,
    CoachFeedback, Timeline, TimelineItem, TimelineActivePointer, QuizAttempt, QuizItem, QuizReferenceAnswer, utcnow)
from spinread.core.access import audit, viewable_video, coach_can_view, require_coach
from spinread.product.report_policy import public_plan_item
from spinread.core.mutations import mutate, version_check

router = APIRouter(prefix="/api", tags=["coaching"])
DB = Annotated[Session, Depends(get_db)]
Actor = Annotated[User, Depends(get_current_user)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=100)]


def grant_out(db, g):
    coach, player = db.get(User, g.coach_id), db.get(User, g.player_id)
    return {"id": g.id, "player_id": g.player_id, "player_name": player.display_name,
        "coach_id": g.coach_id, "coach_name": coach.display_name, "coach_email": coach.email,
        "status": g.status, "version": g.version}


@router.get("/coach-grants")
def grants(db: DB, user: Actor):
    rows = db.scalars(select(CoachGrant).where((CoachGrant.player_id == user.id) | (CoachGrant.coach_id == user.id)))
    return {"items": [grant_out(db, g) for g in rows]}


class GrantIn(BaseModel):
    coach_email: str = Field(min_length=3, max_length=254)


@router.post("/coach-grants")
def grant(body: GrantIn, db: DB, user: Actor, key: Key):
    coach = db.scalar(select(User).where(User.email == body.coach_email.strip().lower(), User.role == "COACH"))
    if coach is None or coach.id == user.id:
        raise ApiError(422, "COACH_NOT_FOUND", "请输入另一位已注册教练的邮箱")
    def action():
        g = db.scalar(select(CoachGrant).where(CoachGrant.player_id == user.id, CoachGrant.coach_id == coach.id))
        if g is None:
            g = CoachGrant(player_id=user.id, coach_id=coach.id)
            db.add(g)
        elif g.status != "ACTIVE":
            g.status, g.revoked_at, g.version = "ACTIVE", None, g.version + 1
        db.flush()
        audit(db, user.id, "COACH_GRANT", g.id, coach_id=coach.id)
        return grant_out(db, g)
    return mutate(db, user, key, "coach-grant", body.model_dump(), action)


class VersionIn(BaseModel):
    base_version: int = Field(ge=1)


def cancel_open_reviews(db, video_ids, coach_id):
    rows = db.scalars(select(ReviewRequest).where(ReviewRequest.video_id.in_(video_ids),
        ReviewRequest.coach_id == coach_id, ReviewRequest.status.in_(["OPEN", "CLAIMED"])))
    for r in rows:
        r.status, r.version = "CANCELLED", r.version + 1


@router.post("/coach-grants/{grant_id}/revoke")
def revoke_grant(grant_id: str, body: VersionIn, db: DB, user: Actor, key: Key):
    g = db.get(CoachGrant, grant_id)
    if g is None or g.player_id != user.id:
        raise not_found("教练授权不存在")
    def action():
        db.refresh(g, with_for_update=True)
        version_check(g.version, body.base_version)
        g.status, g.revoked_at, g.version = "REVOKED", utcnow(), g.version + 1
        consents = db.scalars(select(ConsentGrant).where(ConsentGrant.subject_user_id == user.id,
            ConsentGrant.granted_to == g.coach_id, ConsentGrant.state == "ACTIVE")).all()
        for c in consents:
            c.state, c.revoked_at, c.version = "REVOKED", utcnow(), c.version + 1
            audit(db, user.id, "CONSENT_REVOKED", c.id, c.video_id, cause="COACH_GRANT_REVOKED")
        cancel_open_reviews(db, [c.video_id for c in consents], g.coach_id)
        audit(db, user.id, "COACH_GRANT_REVOKED", g.id)
        return grant_out(db, g)
    return mutate(db, user, key, f"revoke-grant:{grant_id}", body.model_dump(), action)


def consent_out(c):
    return {"id": c.id, "video_id": c.video_id, "purpose": c.purpose,
        "granted_to": c.granted_to, "state": c.state, "version": c.version}


@router.get("/videos/{video_id}/consents")
def consents(video_id: str, db: DB, user: Actor):
    get_owned_video(video_id, db, user)
    return {"items": [consent_out(c) for c in db.scalars(select(ConsentGrant).where(ConsentGrant.video_id == video_id))]}


class ConsentIn(BaseModel):
    purpose: Literal["COACH_VIEW"]
    granted_to: str


@router.post("/videos/{video_id}/consents")
def consent(video_id: str, body: ConsentIn, db: DB, user: Actor, key: Key):
    get_owned_video(video_id, db, user)
    def action():
        relation = db.scalar(select(CoachGrant).where(CoachGrant.player_id == user.id,
            CoachGrant.coach_id == body.granted_to, CoachGrant.status == "ACTIVE").with_for_update())
        if relation is None:
            raise forbidden("请先建立教练关系授权")
        c = db.scalar(select(ConsentGrant).where(ConsentGrant.video_id == video_id,
            ConsentGrant.granted_to == body.granted_to, ConsentGrant.purpose == body.purpose))
        if c is None:
            c = ConsentGrant(video_id=video_id, subject_user_id=user.id, **body.model_dump())
            db.add(c)
        elif c.state != "ACTIVE":
            c.state, c.revoked_at, c.granted_at, c.version = "ACTIVE", None, utcnow(), c.version + 1
        db.flush()
        audit(db, user.id, "CONSENT_GRANTED", c.id, video_id, purpose=c.purpose, granted_to=c.granted_to)
        return consent_out(c)
    return mutate(db, user, key, f"consent:{video_id}", body.model_dump(), action)


@router.post("/consents/{consent_id}/revoke")
def revoke_consent(consent_id: str, body: VersionIn, db: DB, user: Actor, key: Key):
    c = db.get(ConsentGrant, consent_id)
    if c is None or c.subject_user_id != user.id:
        raise not_found("视频许可不存在")
    def action():
        db.refresh(c, with_for_update=True)
        version_check(c.version, body.base_version)
        c.state, c.revoked_at, c.version = "REVOKED", utcnow(), c.version + 1
        cancel_open_reviews(db, [c.video_id], c.granted_to)
        audit(db, user.id, "CONSENT_REVOKED", c.id, c.video_id)
        return consent_out(c)
    return mutate(db, user, key, f"revoke-consent:{consent_id}", body.model_dump(), action)


def review_out(db, r, detail=False):
    video, coach = db.get(Video, r.video_id), db.get(User, r.coach_id)
    pointer = db.get(TimelineActivePointer, video.id)
    tl = db.get(Timeline, pointer.timeline_id) if pointer else None
    out = {"id": r.id, "video_id": r.video_id, "filename": video.filename,
        "player_id": video.owner_id, "player_name": db.get(User, video.owner_id).display_name,
        "coach_id": coach.id, "coach_name": coach.display_name, "status": r.status,
        "version": r.version, "timeline_version": r.timeline_version, "question": r.question,
        "stale": tl is None or tl.version != r.timeline_version, "created_at": r.created_at}
    if detail:
        from spinread.product.replay_training import rally_for_item, reference_out
        from spinread.api.routers.replay import review_data
        current_artifact_id = review_data(db,video,tl.version)['analysis_artifact_id'] if tl else None
        rows = db.scalars(select(CoachFeedback).where(CoachFeedback.review_request_id == r.id).order_by(CoachFeedback.created_at))
        out["feedback"] = [{"id": f.id, "body": f.body, "start_ms": f.start_ms, "end_ms": f.end_ms,
            "timeline_item_id": f.timeline_item_id, "provenance": f.provenance, "created_at": f.created_at} for f in rows]
        attempts = db.execute(select(QuizAttempt, QuizItem).join(QuizItem).where(QuizItem.video_id == video.id,
            QuizAttempt.user_id == video.owner_id).order_by(QuizAttempt.created_at.desc()).limit(50)).all()
        out["practice_notes"] = []
        for a, i in attempts:
            rally = rally_for_item(db, i)
            out["practice_notes"].append({"start_ms":i.start_ms, "contact_ms":i.contact_ms,
                "pause_ms":i.pause_ms,"end_ms":i.end_ms,"quiz_item_id":i.id,"item_version":i.version,
                "timeline_version":db.get(Timeline,i.timeline_id).version,
                "stale":i.timeline_id != (tl.id if tl else None) or not i.is_current,
                "replay_artifact_id":current_artifact_id if tl and i.timeline_id == tl.id and current_artifact_id
                    else ((rally.attributes or {}).get('replay_artifact_id') if rally else None),
                "rally_id":rally.id if rally else None,"answer":a.answer,
                "confidence":a.confidence,"created_at":a.created_at})
        out["reference_answers"] = [reference_out(db, a) for a in db.scalars(
            select(QuizReferenceAnswer).where(QuizReferenceAnswer.review_request_id == r.id)
            .order_by(QuizReferenceAnswer.created_at.desc(), QuizReferenceAnswer.id.desc()))]
    return out


class ReviewIn(BaseModel):
    video_id: str
    coach_id: str
    question: str = Field(default="", max_length=4000)


@router.post("/review-requests")
def request_review(body: ReviewIn, db: DB, user: Actor, key: Key):
    video = get_owned_video(body.video_id, db, user)
    def action():
        coach = db.get(User, body.coach_id)
        if coach is None or not coach_can_view(db, coach, video):
            raise forbidden("请先明确授权该教练查看此视频")
        pointer = db.get(TimelineActivePointer, video.id)
        if pointer is None:
            raise ApiError(409, "NO_TIMELINE", "请等待视频分析完成")
        existing = db.scalar(select(ReviewRequest).where(ReviewRequest.video_id == video.id,
            ReviewRequest.coach_id == coach.id, ReviewRequest.status.in_(["OPEN", "CLAIMED"])))
        if existing:
            raise ApiError(409, "REVIEW_ALREADY_OPEN", "此教练已有待处理的评审请求")
        r = ReviewRequest(video_id=video.id, coach_id=coach.id, question=body.question,
            timeline_version=db.get(Timeline, pointer.timeline_id).version)
        db.add(r); db.flush()
        audit(db, user.id, "REVIEW_REQUESTED", r.id, video.id)
        return review_out(db, r)
    return mutate(db, user, key, "review-request", body.model_dump(), action)


@router.get("/review-requests")
def own_reviews(db: DB, user: Actor):
    rows = db.scalars(select(ReviewRequest).join(Video).where(Video.owner_id == user.id,
        Video.deleted_at.is_(None)).order_by(ReviewRequest.created_at.desc()))
    return {"items": [review_out(db, r) for r in rows]}


@router.get("/coaches/{coach_id}/review-queue")
def review_queue(coach_id: str, db: DB, user: Actor):
    require_coach(user)
    if coach_id != user.id:
        raise forbidden()
    rows = db.scalars(select(ReviewRequest).join(Video).where(ReviewRequest.coach_id == user.id,
        Video.deleted_at.is_(None)).order_by(ReviewRequest.created_at.desc()))
    return {"items": [review_out(db, r) for r in rows if coach_can_view(db, user, db.get(Video, r.video_id))]}


def owned_review(db, user, request_id):
    r = db.get(ReviewRequest, request_id)
    if r is None:
        raise not_found("评审不存在")
    v = viewable_video(r.video_id, db, user)
    if user.id not in (v.owner_id, r.coach_id):
        raise forbidden()
    return r, v


@router.get("/review-requests/{request_id}")
def review_detail(request_id: str, db: DB, user: Actor):
    r, _ = owned_review(db, user, request_id)
    return review_out(db, r, detail=True)


class FeedbackIn(VersionIn):
    body: str = Field(min_length=1, max_length=8000)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)
    timeline_item_id: str | None = None
    complete: bool = False

    @model_validator(mode="after")
    def bounds(self):
        if (self.start_ms is None) != (self.end_ms is None) or (self.start_ms is not None and self.end_ms <= self.start_ms):
            raise ValueError("批注需要完整且有序的开始、结束时间")
        return self


class ReferenceAnswer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    spin: Literal['UNKNOWN','TOPSPIN','BACKSPIN','SIDESPIN','SIDE_TOP','SIDE_BACK','NO_SPIN'] = 'UNKNOWN'
    length: Literal['UNKNOWN','SHORT','HALF_LONG','LONG'] = 'UNKNOWN'
    acceptable_receives: list[Literal['PUSH','FLICK','TOPSPIN','BLOCK','CHOP']] = Field(default_factory=list,max_length=5)
    note: str = Field(default='',max_length=4000)


class ReferenceEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rally_id: str
    contact_ms: int = Field(ge=0)
    replay_artifact_id: str | None = None


class ReferenceAnswerIn(VersionIn):
    model_config = ConfigDict(extra='forbid')
    quiz_item_id: str
    quiz_item_version: int = Field(ge=1)
    timeline_version: int = Field(ge=1)
    answer: ReferenceAnswer
    evidence: ReferenceEvidence
    diagnosis: Literal['READING','EXECUTION','MIXED','UNKNOWN'] = 'UNKNOWN'


@router.post('/review-requests/{request_id}/quiz-reference-answers')
def quiz_reference(request_id: str, body: ReferenceAnswerIn, db: DB, user: Actor, key: Key):
    require_coach(user)
    review, video = owned_review(db,user,request_id)
    if review.coach_id != user.id:
        raise forbidden()
    def action():
        if not coach_can_view(db,user,video,lock=True):
            raise forbidden()
        db.refresh(review,with_for_update=True)
        version_check(review.version,body.base_version)
        if review.status not in ('OPEN','CLAIMED'):
            raise ApiError(409,'REVIEW_CLOSED','此评审已结束')
        pointer = db.scalar(select(TimelineActivePointer).where(TimelineActivePointer.video_id == video.id).with_for_update())
        tl = db.get(Timeline,pointer.timeline_id) if pointer else None
        item = db.scalar(select(QuizItem).where(QuizItem.id == body.quiz_item_id).with_for_update())
        if (tl is None or body.timeline_version != tl.version or review.timeline_version != tl.version
                or item is None or item.video_id != video.id or item.timeline_id != tl.id
                or not item.is_current or item.version != body.quiz_item_version):
            raise ApiError(409,'QUIZ_VERSION_CONFLICT','题目、评审或时间线已更新，请刷新后重新核对。')
        if item.approval != 'APPROVED' or db.scalar(select(QuizAttempt.id).where(
                QuizAttempt.quiz_item_id == item.id, QuizAttempt.user_id == video.owner_id).limit(1)) is None:
            raise ApiError(409,'QUIZ_NOT_ATTEMPTED','请先由球员确认片段并提交判断。')
        rally = db.get(TimelineItem,body.evidence.rally_id)
        if (rally is None or rally.timeline_id != tl.id or rally.type != 'RALLY' or rally.status != 'ACTIVE'
                or not rally.start_ms <= item.contact_ms < rally.end_ms
                or not max(item.start_ms,rally.start_ms) <= body.evidence.contact_ms < min(item.end_ms,rally.end_ms)):
            raise ApiError(422,'INVALID_REFERENCE_EVIDENCE','证据必须属于这道题的当前回合。')
        hit = db.scalar(select(TimelineItem.id).where(TimelineItem.timeline_id == tl.id,
            TimelineItem.parent_id == rally.id,TimelineItem.type == 'HIT_CANDIDATE',
            TimelineItem.status == 'ACTIVE',TimelineItem.start_ms == body.evidence.contact_ms).limit(1))
        if hit is None:
            raise ApiError(422,'INVALID_REFERENCE_EVIDENCE','请选择当前回合中仍有效的逐拍时刻。')
        previous = db.scalar(select(QuizReferenceAnswer).where(QuizReferenceAnswer.quiz_item_id == item.id,
            QuizReferenceAnswer.coach_id == user.id).order_by(QuizReferenceAnswer.version.desc()).limit(1))
        from spinread.api.routers.replay import review_data
        artifact_id = review_data(db,video,tl.version)['analysis_artifact_id'] or (rally.attributes or {}).get('replay_artifact_id')
        if body.evidence.replay_artifact_id != artifact_id:
            raise ApiError(409,'REPLAY_EVIDENCE_CONFLICT','分析证据已更新，请刷新并核对后再确认参考答案。')
        answer = QuizReferenceAnswer(quiz_item_id=item.id,review_request_id=review.id,coach_id=user.id,
            version=previous.version+1 if previous else 1,answer=body.answer.model_dump(),diagnosis=body.diagnosis,
            evidence={**body.evidence.model_dump(),'timeline_id':tl.id,'timeline_version':tl.version,
                'timeline_hit_id':hit,'replay_artifact_id':artifact_id,
                'source':'COACH_REVIEW'})
        db.add(answer);db.flush()
        review.status,review.version = 'CLAIMED',review.version+1
        audit(db,user.id,'QUIZ_REFERENCE_CONFIRMED',answer.id,video.id,quiz_item_id=item.id,version=answer.version)
        return review_out(db,review,detail=True)
    return mutate(db,user,key,f'quiz-reference:{request_id}',body.model_dump(),action)


@router.post("/review-requests/{request_id}/feedback")
def feedback(request_id: str, body: FeedbackIn, db: DB, user: Actor, key: Key):
    require_coach(user)
    r, v = owned_review(db, user, request_id)
    if r.coach_id != user.id:
        raise forbidden()
    def action():
        if not coach_can_view(db, user, v, lock=True): raise forbidden()
        db.refresh(r, with_for_update=True)
        version_check(r.version, body.base_version)
        if r.status not in ("OPEN", "CLAIMED"):
            raise ApiError(409, "REVIEW_CLOSED", "此评审已结束")
        if body.end_ms is not None and body.end_ms > (v.duration_ms or 0):
            raise ApiError(422, "INVALID_BOUNDS", "批注时间超出视频")
        if body.timeline_item_id:
            item = db.get(TimelineItem, body.timeline_item_id)
            tl = db.get(Timeline, item.timeline_id) if item else None
            if tl is None or tl.video_id != v.id or tl.version != r.timeline_version:
                raise ApiError(422, "INVALID_ITEM", "批注片段必须属于本次评审的时间线版本")
        f = CoachFeedback(review_request_id=r.id, body=body.body, start_ms=body.start_ms,
            end_ms=body.end_ms, timeline_item_id=body.timeline_item_id,
            provenance={"source": "COACH", "source_id": user.id, "timeline_version": r.timeline_version})
        db.add(f)
        r.status, r.version = "DONE" if body.complete else "CLAIMED", r.version + 1
        db.flush()
        audit(db, user.id, "COACH_FEEDBACK", f.id, v.id, review_request_id=r.id)
        return review_out(db, r, detail=True)
    return mutate(db, user, key, f"feedback:{request_id}", body.model_dump(), action)


@router.post("/review-requests/{request_id}/cancel")
def cancel_review(request_id: str, body: VersionIn, db: DB, user: Actor, key: Key):
    r, v = owned_review(db, user, request_id)
    if v.owner_id != user.id:
        raise forbidden("只有球员能取消评审")
    def action():
        db.refresh(r, with_for_update=True)
        version_check(r.version, body.base_version)
        if r.status not in ("OPEN", "CLAIMED"):
            raise ApiError(409, "REVIEW_CLOSED", "评审已经结束")
        r.status, r.version = "CANCELLED", r.version + 1
        audit(db, user.id, "REVIEW_CANCELLED", r.id, v.id)
        return review_out(db, r)
    return mutate(db, user, key, f"cancel-review:{request_id}", body.model_dump(), action)


@router.get("/coaches/{coach_id}/dashboard")
def dashboard(coach_id: str, db: DB, user: Actor):
    """Aggregate only currently shared resources; counts obey the same scope as detail."""
    from spinread.core.models import AnalysisReport, TrainingPlan, PlanItem, PlanItemRetest
    require_coach(user)
    if coach_id != user.id:
        raise forbidden()
    relations = db.scalars(select(CoachGrant).where(CoachGrant.coach_id == user.id,
        CoachGrant.status == "ACTIVE").order_by(CoachGrant.created_at)).all()
    shared = db.scalars(select(Video).join(ConsentGrant, ConsentGrant.video_id == Video.id)
        .join(CoachGrant, CoachGrant.player_id == Video.owner_id).where(
            ConsentGrant.granted_to == user.id, ConsentGrant.purpose == "COACH_VIEW",
            ConsentGrant.state == "ACTIVE", CoachGrant.coach_id == user.id,
            CoachGrant.status == "ACTIVE", Video.deleted_at.is_(None)).order_by(Video.created_at.desc())).all()
    video_ids = {v.id for v in shared}
    requests = db.scalars(select(ReviewRequest).where(ReviewRequest.coach_id == user.id,
        ReviewRequest.video_id.in_(video_ids)).order_by(ReviewRequest.created_at)).all()
    tasks = db.execute(select(PlanItem, AnalysisReport.video_id).join(AnalysisReport,
        PlanItem.source_report_id == AnalysisReport.id).join(TrainingPlan, PlanItem.plan_id == TrainingPlan.id)
        .where(AnalysisReport.video_id.in_(video_ids), TrainingPlan.state == "ACTIVE")
        .order_by(PlanItem.updated_at.desc())).all()
    tasks = [(item, vid) for item, vid in tasks if public_plan_item(db, item)]
    task_ids = {item.id for item, _ in tasks}
    retests = db.scalars(select(PlanItemRetest).where(PlanItemRetest.plan_item_id.in_(task_ids),
        PlanItemRetest.video_id.in_(video_ids)).order_by(PlanItemRetest.created_at.desc())).all()
    players = []
    for relation in relations:
        player = db.get(User, relation.player_id)
        videos = [v for v in shared if v.owner_id == player.id]
        ids = {v.id for v in videos}
        player_tasks = [item for item, vid in tasks if vid in ids]
        ids_tasks = {i.id for i in player_tasks}
        player_retests = [r for r in retests if r.plan_item_id in ids_tasks]
        reviews = [r for r in requests if r.video_id in ids]
        players.append({"id": player.id, "name": player.display_name,
            "videos": [{"id": v.id, "filename": v.filename, "state": v.state, "created_at": v.created_at} for v in videos],
            "pending_reviews": sum(r.status in ("OPEN", "CLAIMED") for r in reviews),
            "progress": {state: sum(i.status == state for i in player_tasks) for state in ("ACTIVE", "DONE", "DROPPED")},
            "tasks": [{"id": i.id, "title": i.title, "status": i.status, "player_note": i.player_note,
                "updated_at": i.updated_at, "video_id": next(vid for item, vid in tasks if item.id == i.id)} for i in player_tasks],
            "retests": [{"id": r.id, "task_id": r.plan_item_id, "created_at": r.created_at,
                "comparable": r.result.get("comparable", False), "success": r.result.get("success")} for r in player_retests]})
    return {"players": players, "reviews": [review_out(db, r) for r in requests],
        "summary": {"players": len(players), "pending_reviews": sum(r.status in ("OPEN", "CLAIMED") for r in requests),
            "active_tasks": sum(i.status == "ACTIVE" for i, _ in tasks), "retests": len(retests)}}

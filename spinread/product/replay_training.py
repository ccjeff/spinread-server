"""Versioned links between replay, private quizzes and human coaching."""
from sqlalchemy import select
from spinread.core.models import QuizItem, QuizReferenceAnswer, TimelineItem


def reference_out(db, answer):
    item = db.get(QuizItem, answer.quiz_item_id)
    return {'id':answer.id, 'quiz_item_id':answer.quiz_item_id,
            'quiz_item_version':item.version, 'review_request_id':answer.review_request_id,
            'coach_id':answer.coach_id, 'version':answer.version, 'answer':answer.answer,
            'evidence':answer.evidence, 'diagnosis':answer.diagnosis, 'created_at':answer.created_at}


def references(db, quiz_item_id):
    rows = db.scalars(select(QuizReferenceAnswer).where(QuizReferenceAnswer.quiz_item_id == quiz_item_id)
        .order_by(QuizReferenceAnswer.created_at.desc(), QuizReferenceAnswer.id.desc())).all()
    return [reference_out(db, r) for r in rows]


def rally_for_item(db, item):
    """Anchor to an active rally in the exact immutable quiz timeline."""
    requested = (item.provenance or {}).get('rally_id')
    query = select(TimelineItem).where(TimelineItem.timeline_id == item.timeline_id,
        TimelineItem.type == 'RALLY', TimelineItem.status == 'ACTIVE',
        TimelineItem.start_ms <= item.contact_ms, TimelineItem.end_ms > item.contact_ms)
    if requested:
        query = query.where(TimelineItem.id == requested)
    return db.scalar(query.order_by(TimelineItem.start_ms, TimelineItem.id).limit(1))

"""Recording-local practice context. Reviewed labels are never model output."""
from sqlalchemy import select
from pingpong_training.analysis.practice import CLASSIFIER_VERSION, FEATURE_VERSION, PracticeClassifier
from spinread.core.models import PracticeWindow


def practice_context(db, video_id, timeline_id):
    rows = db.scalars(select(PracticeWindow).where(PracticeWindow.video_id == video_id,
        PracticeWindow.timeline_id == timeline_id, PracticeWindow.feature_version == FEATURE_VERSION)
        .order_by(PracticeWindow.start_ms)).all()
    model = PracticeClassifier([(r.features, r.label) for r in rows if r.label])
    windows = []
    for r in rows:
        prediction = model.predict(r.features)
        decision = prediction if r.label is None else {"type": r.label, "source": r.label_source,
            "reason": "REVIEWED", "margin": None, "model_version": CLASSIFIER_VERSION}
        windows.append({"id": r.id, "start_ms": r.start_ms, "end_ms": r.end_ms,
            "revision": r.revision, "label": r.label, "decision": decision, "prediction": prediction})
    return {"model": {"version": CLASSIFIER_VERSION, "ready": model.ready,
        "examples": model.counts, "scope": "THIS_RECORDING", "calibrated": False}, "windows": windows}


def context_for_contact(context, contact_ms):
    return next((w for w in context["windows"] if w["start_ms"] <= contact_ms < w["end_ms"]), None)

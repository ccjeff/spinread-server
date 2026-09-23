"""Transaction-local audit and 24h idempotency for the collaboration APIs."""
import hashlib
import json
from datetime import timedelta, timezone
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from spinread.core.models import IdempotencyKey, User, utcnow
from spinread.api.errors import ApiError


def mutate(db, user, key, operation, payload, action):
    # Serialize each actor's mutations so concurrent retries cannot both run.
    # Resource authorization MUST precede this helper, including on replay.
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    digest = hashlib.sha256(json.dumps([operation, payload], sort_keys=True).encode()).hexdigest()
    row = db.scalar(select(IdempotencyKey).where(IdempotencyKey.user_id == user.id, IdempotencyKey.key == key))
    if row:
        created = row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at
        if created < utcnow() - timedelta(hours=24):
            db.delete(row)
            db.flush()
        elif row.request_hash != digest:
            raise ApiError(409, "IDEMPOTENCY_KEY_REUSE", "重复请求的内容不一致")
        else:
            return row.response_json
    result = jsonable_encoder(action())
    db.add(IdempotencyKey(user_id=user.id, key=key, request_hash=digest, response_json=result))
    db.flush()
    return result


def version_check(actual, expected):
    if actual != expected:
        raise ApiError(409, "VERSION_CONFLICT", "内容已更新，请刷新后重新编辑", {"current_version": actual})

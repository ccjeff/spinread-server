"""Timeline editing (HLD §8.6): apply ops to the active timeline, publish a
new version, switch the pointer, seed a CORRECTION run (METRICS/REPORT only).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.core.models import (
    PipelineRun,
    Timeline,
    TimelineActivePointer,
    TimelineItem,
    Video,
    utcnow,
)
from spinread.pipeline import orchestrator

ACTIVITY_TOP_TYPES = {"RALLY_LIKE", "BALL_PICKUP", "BREAK", "INSTRUCTION", "UNKNOWN"}
CORRECTION_STAGES = {"METRICS", "REPORT"}


class EditError(Exception):
    """Unprocessable edit (validation failed). HTTP 422."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass
class _Node:
    id: str
    parent_id: str | None
    type: str
    start_ms: int
    end_ms: int
    actor: str | None
    attributes: dict
    confidence: float | None
    provenance: dict
    children: list["_Node"] = field(default_factory=list)


def _load_tree(session: Session, timeline_id: str) -> list[_Node]:
    rows = session.scalars(
        select(TimelineItem)
        .where(TimelineItem.timeline_id == timeline_id, TimelineItem.status == "ACTIVE")
        .order_by(TimelineItem.start_ms)
    ).all()
    nodes: dict[str, _Node] = {}
    roots: list[_Node] = []
    for r in rows:
        nodes[r.id] = _Node(
            id=r.id, parent_id=r.parent_id, type=r.type,
            start_ms=r.start_ms, end_ms=r.end_ms, actor=r.actor,
            attributes=dict(r.attributes or {}), confidence=r.confidence,
            provenance=dict(r.provenance or {}),
        )
    for r in rows:
        node = nodes[r.id]
        if r.parent_id and r.parent_id in nodes:
            nodes[r.parent_id].children.append(node)
        else:
            node.parent_id = None
            roots.append(node)
    roots.sort(key=lambda n: n.start_ms)
    return roots


def _find(roots: list[_Node], item_id: str) -> tuple[_Node | None, list[_Node]]:
    """Locate a node anywhere; returns (node, its siblings list)."""
    stack = [(n, roots) for n in roots]
    while stack:
        node, siblings = stack.pop()
        if node.id == item_id:
            return node, siblings
        stack.extend((c, node.children) for c in node.children)
    return None, roots


def _clamp_subtree(node: _Node) -> None:
    """Constrain children to the node's (new) interval, recursively.

    Children fully inside keep their exact interval; partially overlapping
    ones are truncated; fully outside ones are dropped (in full-copy timeline
    versioning, absence from the new version IS the REMOVED semantics).
    """
    kept: list[_Node] = []
    for child in node.children:
        s = max(child.start_ms, node.start_ms)
        e = min(child.end_ms, node.end_ms)
        if e <= s:
            continue  # outside the new bounds: subtree goes with it
        child.start_ms, child.end_ms = s, e
        _clamp_subtree(child)
        kept.append(child)
    node.children = kept


def _apply_ops(roots: list[_Node], ops: list[dict]) -> list[_Node]:
    for op in ops:
        kind = op.get("op")
        item_id = op.get("timeline_item_id")
        node, siblings = _find(roots, item_id or "")
        if node is None:
            raise EditError("ITEM_NOT_FOUND", f"timeline item {item_id} not found")

        if kind == "UPDATE_BOUNDARY":
            node.start_ms = int(op["start_ms"])
            node.end_ms = int(op["end_ms"])
            _clamp_subtree(node)  # out-of-bounds descendants are REMOVED, not clamped to nothing

        elif kind == "SET_LABEL":
            if op.get("field") != "type":
                raise EditError("UNSUPPORTED_FIELD", "only field='type' is editable")
            if node.parent_id is not None:
                raise EditError(
                    "LABEL_NOT_EDITABLE",
                    "only top-level activity segments can be relabeled",
                )
            value = str(op.get("value", ""))
            if value not in ACTIVITY_TOP_TYPES:
                raise EditError("INVALID_LABEL", f"type must be one of {sorted(ACTIVITY_TOP_TYPES)}")
            node.type = value

        elif kind == "SPLIT":
            if node.parent_id is not None:
                raise EditError("SPLIT_NOT_ALLOWED", "only top-level segments can be split")
            at = int(op["at_ms"])
            if not (node.start_ms < at < node.end_ms):
                raise EditError("SPLIT_OUT_OF_RANGE", "at_ms must be strictly inside the segment")
            right = _Node(
                id=node.id, parent_id=None, type=node.type,
                start_ms=at, end_ms=node.end_ms, actor=node.actor,
                attributes=dict(node.attributes), confidence=node.confidence,
                provenance=dict(node.provenance),
            )
            left_children, right_children = [], []
            for child in node.children:
                if child.start_ms >= at:
                    child.end_ms = min(child.end_ms, right.end_ms)
                    right_children.append(child)
                else:
                    child.end_ms = min(child.end_ms, at)
                    if child.end_ms > child.start_ms:
                        left_children.append(child)
                    else:  # degenerate after clamp: move to the right side
                        child.start_ms, child.end_ms = at, min(at + 1, right.end_ms)
                        right_children.append(child)
            node.children = left_children
            right.children = right_children
            node.end_ms = at
            idx = siblings.index(node)
            siblings.insert(idx + 1, right)

        elif kind == "MERGE_NEXT":
            if node.parent_id is not None:
                raise EditError("MERGE_NOT_ALLOWED", "only top-level segments can be merged")
            idx = roots.index(node)
            if idx + 1 >= len(roots):
                raise EditError("NO_NEXT_SEGMENT", "no following segment to merge with")
            nxt = roots[idx + 1]
            if nxt.type != node.type:
                raise EditError(
                    "MERGE_TYPE_MISMATCH",
                    "next segment has a different type; relabel first",
                )
            node.end_ms = nxt.end_ms
            node.children.extend(nxt.children)
            node.children.sort(key=lambda c: c.start_ms)
            roots.remove(nxt)

        elif kind == "DELETE":
            siblings.remove(node)  # subtree goes with it

        else:
            raise EditError("UNKNOWN_OP", f"unsupported op {kind!r}")
    return roots


def _validate(roots: list[_Node], duration_ms: int | None) -> int:
    n = 0
    prev_end = -1
    for root in roots:
        if root.end_ms <= root.start_ms:
            raise EditError("INVALID_INTERVAL", f"segment {root.id}: end must be > start")
        if root.start_ms < 0 or (duration_ms and root.end_ms > duration_ms):
            raise EditError(
                "OUT_OF_RANGE", f"segment {root.id} outside [0, {duration_ms})"
            )
        if root.start_ms < prev_end:
            raise EditError(
                "OVERLAPPING_SEGMENTS",
                f"top-level segment {root.id} overlaps its predecessor",
            )
        prev_end = root.end_ms
        n += 1
        for child in root.children:
            if child.end_ms <= child.start_ms:
                raise EditError("INVALID_INTERVAL", f"item {child.id}: end must be > start")
            n += 1
            for grand in child.children:
                if grand.end_ms <= grand.start_ms:
                    raise EditError("INVALID_INTERVAL", f"item {grand.id}: end must be > start")
                n += 1
    return n


def apply_timeline_edits(
    session: Session, video: Video, base_timeline_version: int, ops: list[dict]
) -> tuple[Timeline, int]:
    """Returns (new timeline, n_items). Raises EditError on validation failure."""
    pointer = session.get(TimelineActivePointer, video.id)
    if pointer is None:
        raise EditError("NO_TIMELINE", "video has no active timeline")
    current = session.get(Timeline, pointer.timeline_id)
    if current.version != base_timeline_version:
        raise VersionConflict(current)

    roots = _load_tree(session, current.id)
    roots = _apply_ops(roots, ops)
    n_items = _validate(roots, video.duration_ms)

    new_timeline = Timeline(
        video_id=video.id,
        version=current.version + 1,
        state="PUBLISHED",
        created_by="USER",
    )
    session.add(new_timeline)
    session.flush()

    def emit(node: _Node, parent_row_id: str | None) -> None:
        row = TimelineItem(
            timeline_id=new_timeline.id,
            parent_id=parent_row_id,
            type=node.type,
            start_ms=node.start_ms,
            end_ms=node.end_ms,
            actor=node.actor,
            attributes=node.attributes,
            confidence=node.confidence,
            provenance={"source": "USER", "source_id": "timeline-edits"},
            status="ACTIVE",
        )
        session.add(row)
        session.flush()
        for child in node.children:
            emit(child, row.id)

    for root in roots:
        emit(root, None)

    pointer.timeline_id = new_timeline.id
    pointer.switched_at = utcnow()

    run = PipelineRun(
        video_id=video.id,
        pipeline_version=_pipeline_version(),
        trigger="CORRECTION",
    )
    session.add(run)
    session.flush()
    orchestrator.tick(session, run.id, only_stages=CORRECTION_STAGES)
    return new_timeline, n_items


def _pipeline_version() -> str:
    from spinread.pipeline.dag import PIPELINE_VERSION

    return PIPELINE_VERSION


class VersionConflict(Exception):
    def __init__(self, current: Timeline):
        self.current = current

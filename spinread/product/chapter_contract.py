"""Versioned model input and deterministic matching of human training intent."""
from typing import Literal
import unicodedata

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AnalysisScope(ContractModel):
    video_id: str
    chapter_id: str
    start_ms: int
    end_ms: int
    timeline_version: int
    annotation_version: int


class Target(ContractModel):
    position: Literal['NEAR', 'FAR']
    source: Literal['USER'] = 'USER'


class TrainingIntent(ContractModel):
    feeding: Literal['RALLY', 'MULTIBALL', 'SERVE_RECEIVE', 'OTHER', 'UNKNOWN']
    action_sequence: list[str]
    movement: str
    source: Literal['USER', 'UNKNOWN']


class ChapterInput(ContractModel):
    schema_version: Literal['chapter-analysis-v1'] = 'chapter-analysis-v1'
    scope: AnalysisScope
    target: Target
    training_intent: TrainingIntent
    focus: str
    sample_manifest_id: str
    known_limitations: list[str]


def action_label(action):
    hand = {'FOREHAND': '正手', 'BACKHAND': '反手'}.get(action.get('hand'), '')
    spin = {'TOPSPIN': '上旋', 'BACKSPIN': '下旋', 'SIDESPIN': '侧旋', 'NO_SPIN': '不转'}.get(action.get('incoming_spin'))
    label = hand + action.get('stroke', '')
    if spin:
        label += f'（人工来球标签：{spin}）'
    if action.get('movement'):
        label += '；' + action['movement']
    return label


def build_input(*, video_id, chapter_id, start_ms, end_ms, timeline_version,
                annotation_version, target, annotation, focus, manifest_id):
    limitations = ['仅对所提供画面形成判断，不能外推整段或总体能力。']
    # An annotation describes its own target; changing the analysis target must
    # not silently transfer that player's action sequence to the other player.
    if annotation and annotation.get('target') != target:
        annotation = None
        limitations.append('训练标注未明确属于本次目标球员，不将其动作标签用于本次评价。')
    if annotation:
        intent = TrainingIntent(feeding=annotation['feeding'],
            action_sequence=[action_label(a) for a in annotation.get('actions', [])],
            movement=annotation.get('movement', 'UNSPECIFIED'), source='USER')
        limitations.append('训练类型和动作顺序来自人工训练标注，未由画面独立验证。')
        if any(a.get('incoming_spin', 'UNKNOWN') != 'UNKNOWN' for a in annotation.get('actions', [])):
            limitations.append('旋转标签来自人工训练标注，未由画面独立验证。')
    else:
        intent = TrainingIntent(feeding='UNKNOWN', action_sequence=[], movement='UNSPECIFIED', source='UNKNOWN')
        limitations.append('缺少该球员的人工训练意图，不能假定具体动作或来球旋转。')
    return ChapterInput(scope=AnalysisScope(video_id=video_id, chapter_id=chapter_id,
        start_ms=start_ms, end_ms=end_ms, timeline_version=timeline_version, annotation_version=annotation_version),
        target=Target(position=target), training_intent=intent, focus=focus,
        sample_manifest_id=manifest_id, known_limitations=limitations)


def intent_signature(segment):
    """No title/position matching: require explicit, ordered action annotations."""
    if not segment or segment.get('target') not in ('NEAR', 'FAR'):
        return None
    actions = segment.get('actions', [])
    if not actions or any(not a.get('stroke', '').strip() or a.get('hand', 'UNSPECIFIED') == 'UNSPECIFIED' for a in actions):
        return None
    def normalized(value):
        return unicodedata.normalize('NFKC', value).strip().casefold()
    return (segment['feeding'], segment.get('movement', 'UNSPECIFIED'), tuple(
        tuple(normalized(a.get(k, default)) for k, default in
              [('hand', 'UNSPECIFIED'), ('stroke', ''), ('incoming_spin', 'UNKNOWN'), ('movement', '')])
        for a in actions))

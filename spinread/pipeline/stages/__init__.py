"""Stage registry: DAG stage name -> implementation."""

from spinread.pipeline.stage import Stage
from spinread.pipeline.stages.activity_stage import ActivityStage
from spinread.pipeline.stages.events_stage import EventsStage
from spinread.pipeline.stages.metrics_stage import MetricsStage
from spinread.pipeline.stages.normalize_stage import NormalizeStage
from spinread.pipeline.stages.probe_stage import ProbeStage
from spinread.pipeline.stages.quality_stage import QualityStage
from spinread.pipeline.stages.rally_stage import RallyStage
from spinread.pipeline.stages.report_stage import ReportStage
from spinread.pipeline.stages.timeline_stage import TimelineStage

REGISTRY: dict[str, Stage] = {
    s.stage: s
    for s in (
        ProbeStage(),
        NormalizeStage(),
        QualityStage(),
        ActivityStage(),
        RallyStage(),
        EventsStage(),
        TimelineStage(),
        MetricsStage(),
        ReportStage(),
    )
}

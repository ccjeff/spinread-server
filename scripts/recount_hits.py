"""Recount racket candidates in existing rally boundaries (dry run by default).

Run with python -m scripts.recount_hits VIDEO_ID [--apply --base-version N].
Original media and prior timelines are retained. A new timeline invalidates old
quiz approvals/comparisons through the normal version gate; confirm them again.
"""
import argparse
from pathlib import Path
from sqlalchemy import select
from spinread.config import get_settings
from spinread.core.db import make_session_factory
from spinread.core.models import MediaAsset, Timeline, TimelineActivePointer, TimelineItem
from spinread.core.storage import S3ObjectStore
from spinread.product.hit_recount import recount_hits
from pingpong_training.analysis.rally import detect_rallies_for_video


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video_id');parser.add_argument('--apply',action='store_true');parser.add_argument('--base-version',type=int)
    args=parser.parse_args()
    if args.apply and args.base_version is None: parser.error('--apply requires --base-version')
    settings=get_settings();factory=make_session_factory()
    with factory() as db:
        asset=db.scalar(select(MediaAsset).where(MediaAsset.video_id==args.video_id,MediaAsset.class_=='ORIGINAL',MediaAsset.status=='ACTIVE'))
        if asset is None: raise RuntimeError('No active original')
        root=Path(settings.tmp_dir)/'cache'/asset.content_hash;root.mkdir(parents=True,exist_ok=True)
        local=root/'original'
        if not local.exists():
            tmp=root/'recount-download';S3ObjectStore(settings).download_file(asset.object_key,str(tmp));tmp.replace(local)
        pointer=db.get(TimelineActivePointer,args.video_id);current=db.get(Timeline,pointer.timeline_id)
        version=current.version
        rows=db.scalars(select(TimelineItem).where(TimelineItem.timeline_id==current.id,TimelineItem.type=='RALLY',TimelineItem.status=='ACTIVE').order_by(TimelineItem.start_ms)).all()
    _,hits=detect_rallies_for_video(local,[],work_dir=root/'work',ffmpeg_bin=settings.ffmpeg_bin,ffprobe_bin=settings.ffprobe_bin)
    print('timeline',version,'rallies',len(rows))
    for r in rows: print(r.start_ms,r.end_ms,r.attributes.get('hits'),sum(r.start_ms<=h<r.end_ms for h in hits))
    if args.apply:
        with factory.begin() as db:
            timeline,run=recount_hits(db,args.video_id,args.base_version,hits)
            print('published timeline',timeline.version,'metrics/report run',run.id)

if __name__=='__main__': main()

"""Rebuild rally children with audio and visual reset evidence (dry run by default).

Run with python -m scripts.resegment_rallies VIDEO_ID [--apply --base-version N].
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
from spinread.product.rally_resegment import resegment_rallies
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
        rows=db.scalars(select(TimelineItem).where(TimelineItem.timeline_id==current.id,TimelineItem.parent_id.is_(None),TimelineItem.type=='RALLY_LIKE',TimelineItem.status=='ACTIVE').order_by(TimelineItem.start_ms)).all()
    rallies,hits=detect_rallies_for_video(local,[(r.start_ms,r.end_ms) for r in rows],work_dir=root/'work',ffmpeg_bin=settings.ffmpeg_bin,ffprobe_bin=settings.ffprobe_bin)
    print('timeline',version,'new rallies',len(rallies))
    for r in rallies: print(r.start_ms,r.end_ms,len(r.hits_ms))
    if args.apply:
        with factory.begin() as db:
            timeline,run=resegment_rallies(db,args.video_id,args.base_version,rallies)
            print('published timeline',timeline.version,'metrics/report run',run.id)

if __name__=='__main__': main()

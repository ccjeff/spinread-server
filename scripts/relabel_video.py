"""Queue a complete label rebuild without transcoding existing playback assets.

python -m scripts.relabel_video VIDEO_ID --base-version N --apply
The configured worker must have pingpong-training[vision] and BlurBall weights.
"""
import argparse
from spinread.config import get_settings
from spinread.core.db import make_session_factory
from spinread.pipeline.vision import vision_config, vision_fingerprint
from spinread.product.relabel_video import create_relabel_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id")
    parser.add_argument("--base-version", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    if vision_config(settings) is None:
        parser.error("Configure SPINREAD_BLURBALL_WEIGHTS before visual relabeling")
    print("visual configuration:", vision_fingerprint(settings))
    factory = make_session_factory()
    with factory() as db:
        run = create_relabel_run(db, args.video_id, args.base_version)
        if args.apply:
            db.commit()
            print("queued", run.id)
        else:
            db.rollback()
            print("dry run OK; add --apply to queue")


if __name__ == "__main__":
    main()

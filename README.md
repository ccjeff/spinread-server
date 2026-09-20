# SpinRead Server(MLP 后端)

SpinRead(乒乓球视频分析产品)的 MLP 后端,落地「上传 → 管线 → 时间线 → 流媒体播放」链路。设计依据:`SpinRead MLP — Engineering High-Level Design.md` 与 `SpinRead LLD.md`(裁剪范围见文末)。

## 架构(简)

```
浏览器/客户端
   │  ① POST /api/video-uploads(领预签名分片 URL,API 不过字节)
   │  ② PUT 分片 ───────────────────────────────► MinIO(S3,spinread-media)
   │  ③ POST …/complete ──► API 同步 finalize(HeadObject 校验→ORIGINAL asset)
   │                         └─► pipeline_run + PROBE job 入队(Postgres jobs 表)
   ▼
FastAPI(api 进程)                    Worker(认领循环,可内嵌 api 线程)
   │  /api/videos/*                     │  SKIP LOCKED 认领 → 执行 stage
   │  /api/video-uploads/*              ▼
   │  /api/videos/{id}/stream/*    PROBE→NORMALIZE→QUALITY→ACTIVITY→TIMELINE
   │  /api/videos/{id}/media/proxy      │  (DAG,spinread/pipeline/dag.py)
   │  /api/videos/{id}/thumbs/*         ▼
   └─────────── 媒体网关(鉴权,S3 取字节,非预签名 GET)   artifacts→S3,时间线→PG
```

- 存储 key 布局(HLD §6.3):`users/{uid}/videos/{vid}/original/{upload_id}/{filename}`、`…/derived/{media_version}/{proxy.mp4,stream/*,thumbs/*}`、`…/analysis/{run}/{stage}/*.json`
- 分析算法复用 POC 包 `pingpong_training`(editable 依赖,不重写):`probe_video` / `analyze_video`
- 队列 = Postgres `jobs` 表 + `SELECT … FOR UPDATE SKIP LOCKED`,指数退避(30s×2^n,封顶 30min),最多 5 次(NORMALIZE 3 次)

## 快速开始

```bash
# 1. 基础设施(Postgres 16 + MinIO)
docker compose up -d        # 或 scripts/dev_up.sh(含健康等待 + 迁移)

# 2. Python 3.12 venv(系统 python3.9 不可用)
/path/to/python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
pip install -e ../pingpong-training   # 分析 POC,editable 依赖(pip 无法在依赖里声明相对路径 editable)

# 3. 数据库迁移
alembic upgrade head

# 4. 起服务(embed worker 默认开,SPINREAD_EMBED_WORKER=true)
uvicorn spinread.api.main:app --port 8000
# 也可以分开跑:SPINREAD_EMBED_WORKER=false uvicorn … + python -m spinread.worker.main
```

演示账号(启动时幂等 seed):**`demo@spinread.local` / `spinread-demo`**

配置全部走环境变量,前缀 `SPINREAD_`,默认值即本地 compose 栈;参考 `.env.example`(真实 `.env` 不要提交)。

## 走一遍完整链路

```bash
python scripts/make_sample.py            # 生成 sample/session.mp4(75s 合成训练视频)
TOKEN=$(curl -s localhost:8000/api/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"demo@spinread.local","password":"spinread-demo"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')
# → POST /api/video-uploads 拿分片预签名 URL,用 boto3/httpx 直传 MinIO(参考 tests/test_e2e.py)
# → POST /api/video-uploads/{upload_id}/complete(同步 finalize,返回 state=UPLOADED)
# → 轮询 GET /api/videos/{id}/processing-status 到 READY
# → GET /api/videos/{id}/timelines/active 看 RALLY_LIKE 片段
# → GET /api/videos/{id}/stream/master.m3u8(HLS)/ media/proxy(Range 206)/ thumbs/{ts}.jpg
```

`tests/test_e2e.py` 就是这条链路的可执行版本。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 登录,JWT HS256 12h |
| GET | `/api/health` | `{ok: true}` |
| POST | `/api/video-uploads` | 建 videos(UPLOAD_PENDING)+ upload_sessions,返回全部预签名分片 URL(16MiB/片,24h 有效,上限 4GiB) |
| POST | `/api/video-uploads/{id}/complete` | CompleteMultipartUpload + 同步 finalize(幂等) |
| GET | `/api/video-uploads/{id}` | 上传会话状态 |
| GET | `/api/videos` | 本人的视频列表(created_at desc) |
| GET | `/api/videos/{id}` | 详情(含 probe) |
| DELETE | `/api/videos/{id}` | 软删(DELETED + assets PURGE_SCHEDULED) |
| GET | `/api/videos/{id}/processing-status` | `{state, progress_pct, stages[], limitations[]}`(LLD §9) |
| GET | `/api/videos/{id}/timelines/active` | 激活时间线 + items |
| GET | `/api/videos/{id}/stream/master.m3u8` | HLS 播放列表(段 URI 重写为网关路径) |
| GET | `/api/videos/{id}/stream/{seg}.ts` | HLS 分片 |
| GET | `/api/videos/{id}/media/proxy` | proxy.mp4,支持 Range(206) |
| GET | `/api/videos/{id}/thumbs/{ts_ms}.jpg` | 缩略图(每 5s 一张) |

除 login/health 外全部需要 `Authorization: Bearer`;错误统一 `{error:{code,message,details?}}`。

## 测试

```bash
pytest -q        # 需要 compose 栈已起;test_queue 打真 PG,test_e2e 打真 PG+MinIO+ffmpeg
```

## 与完整 LLD 的裁剪说明

- 只做到「上传 → 切片时间线」链路:DAG 裁为 PROBE→NORMALIZE→QUALITY→ACTIVITY→TIMELINE;无 rally/event 细化、报告、quiz、教练、consent 细化、指标/计划
- 媒体下发走**鉴权媒体网关**(HLD §6.3 的备选方案),不发预签名 GET;上传仍用预签名 PUT 直传
- HLS 单档 720p30(`scale=min(1280,iw)`),无多档自适应、无 contact sheet
- QUALITY 仅基于 probe 元数据产 HLD §7.2 能力集(无 OpenCV 信号)
- ACTIVITY 直接复用 POC `analyze_video`(activity-heuristic-0.1.0),metrics/limitations 原样入 artifact
- 无审计表、无 Idempotency-Key 头机制、无游标分页、物理删除(CLEANUP)留桩未实现
- MinIO 镜像用 `quay.io/minio/minio`(docker.io 在部分网络拉不到,二者同上游)

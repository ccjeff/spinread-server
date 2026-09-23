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
   │  /api/videos/{id}/stream/*    PROBE→NORMALIZE→QUALITY→ACTIVITY→RALLY→EVENTS
   │  /api/videos/{id}/media/proxy   →TIMELINE→METRICS→REPORT
   │  /api/videos/{id}/thumbs/*        │  (DAG v1.1.0,spinread/pipeline/dag.py)
   │  时间线编辑 / 片段导出            ▼
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
| GET | `/api/videos/{id}/timelines/active` | 激活时间线 + items(层级:activity 段 → RALLY → HIT_CANDIDATE) |
| GET | `/api/videos/{id}/timelines/{version}` | 任意版本时间线 |
| POST | `/api/videos/{id}/timeline-edits` | 时间线编辑(HLD §8.6),base 版本冲突 → 409 |
| POST | `/api/videos/{id}/pipeline-runs` | 手动全量重跑(MANUAL_RERUN);有 RUNNING run → 409 |
| GET | `/api/videos/{id}/reports/active` | 当前 PUBLISHED 报告(指标快照 + findings) |
| POST | `/api/clips` | 片段导出(幂等;异步 CLIP_RENDER,proxy 720p) |
| GET | `/api/clips?video_id=` / `/api/clips/{id}` / `/api/clips/{id}/download` | 片段列表/状态/下载(video/mp4) |
| POST | `/api/highlight-reels` | 多区间集锦(按给定顺序 concat) |
| GET | `/api/highlight-reels/{id}` / `.../download` | 集锦状态/下载 |
| GET | `/api/videos/{id}/stream/master.m3u8` | HLS 播放列表(段 URI 重写为网关路径) |
| GET | `/api/videos/{id}/stream/{seg}.ts` | HLS 分片 |
| GET | `/api/videos/{id}/media/proxy` | proxy.mp4,支持 Range(206) |
| GET | `/api/videos/{id}/thumbs/{ts_ms}.jpg` | 缩略图(每 5s 一张) |

除 login/health 外全部需要 `Authorization: Bearer`;错误统一 `{error:{code,message,details?}}`。

## 管线(DAG v1.1.0)

```
PROBE → NORMALIZE → QUALITY → ACTIVITY → RALLY → EVENTS → TIMELINE → METRICS → REPORT
```

- RALLY(`rally-heuristic-0.1.0`,POC):RALLY_LIKE 段内按击球间隔切回合;ACTIVITY/RALLY 共享稳定媒体缓存(`tmp_dir/cache/<original sha>/`,pcm.npy 跨 run 命中)
- EVENTS(`event-flat-0.1.0`):每回合每拍一条 HIT_CANDIDATE(P0 无球员定位,actor=null)
- TIMELINE(`timeline-build-0.2.0`):activity 段为顶层,RALLY 挂段下,HIT 挂回合下;RALLY/EVENTS artifact 缺失时退化为纯 ACTIVITY 层级
- METRICS(`metrics-0.1.0`):有效时长、回合数/时长分布、每回合拍数、段类型分布、置信覆盖、correction_rate → `metric_values`
- REPORT(`report-0.1.0`):指标快照 + 3 条 findings 规则(低置信占比>0.3 / 非回合时间占比>0.25 / >60s 超长回合);无 LLM

## 时间线编辑(HLD §8.6)

`POST /api/videos/{id}/timeline-edits`,body `{base_timeline_version, operations:[...]}`:

- `UPDATE_BOUNDARY` / `SET_LABEL`(仅顶层段可改类型)/ `SPLIT`(子项按 at_ms 归边)/ `MERGE_NEXT`(同类型)/ `DELETE`(含子树)
- 事务内校验(半开区间、顶层不重叠、范围合法,422),整版复制为新 version(created_by=USER),原子切 pointer
- base 不符 → 409 `TIMELINE_VERSION_CONFLICT` + 当前版本;成功后自动开 CORRECTION run(只含 METRICS/REPORT)重算指标与报告

## 片段导出(HLD §9.2–9.3)

- `POST /api/clips`(单区间,pre_roll 800ms/post_roll 1200ms)/ `POST /api/highlight-reels`(多区间按序)
- 幂等:`sha256(video_id|proxy hash|规范区间|preset)` 命中直接复用同一 clip_id
- 渲染:worker `CLIP_RENDER` 用 POC `render_intervals` 从 **proxy(720p)** 精确重编码;后续可加 original 档
- 状态:RENDERING → READY/FAILED;READY 后 `download_url` 网关下载

## 测试

```bash
pytest -q        # 需要 compose 栈已起;全部打真 PG+MinIO+ffmpeg
```

## 与完整 LLD 的裁剪说明

- DAG 为 9 阶段:无 LOCALIZATION/OUTCOME/ADJUDICATION(rally 切分直接由音频撞击间距驱动,HIT actor=null)
- 报告为确定性指标 + 规则 findings,无 LLM 叙述;已有带可比性约束的训练计划与复测，见 docs/training-loop.md
- 已实现私人观察 quiz（详见下文）；已实现教练关系 + 逐视频 COACH_VIEW 授权；其他 consent 用途仍待实现
- 媒体下发走**鉴权媒体网关**(HLD §6.3 的备选方案),不发预签名 GET;上传仍用预签名 PUT 直传
- HLS 单档 720p30(`scale=min(1280,iw)`),无多档自适应、无 contact sheet
- QUALITY 仅基于 probe 元数据产 HLD §7.2 能力集(无 OpenCV 信号)
- ACTIVITY/RALLY 复用 POC 算法(`analyze_video`/`detect_rallies_for_video`),metrics/limitations 原样入 artifact
- 导出只渲染 proxy 档;训练闭环、媒体访问和删除已有审计；训练闭环写操作支持 Idempotency-Key（旧接口尚未全量覆盖）、无游标分页、物理删除(CLEANUP)留桩未实现
- MinIO 镜像用 `quay.io/minio/minio`(docker.io 在部分网络拉不到,二者同上游)


## 发球 quiz（私人观察练习）

已接通候选生成、手动圈段、边界确认、练习、不可变版本与答题记录。
升级需要重新安装相邻 `pingpong-training`、执行 `alembic upgrade head` 并重启 API/worker。
自动候选需视觉确认；无确认答案的练习一律不计分。
接口、版本规则、检测限制和验证说明见 [docs/quiz.md](docs/quiz.md)。

## 训练闭环

迁移到 `0005_training_loop` 后支持球员/教练注册、逐视频授权、评审批注、版本化训练计划和可比性检查后的复测。详见 [训练闭环](docs/training-loop.md) 与 [LLD 实现清单](docs/implementation-status.md)。

# 逐拍、落点与 3D 动作复盘

2026-10-25。基于现有正式三仓库实现；旧 `pingpong-spin-poc` 保持原训练器。

## 流程

`PROBE → NORMALIZE → QUALITY → ACTIVITY → RALLY → EVENTS → TIMELINE → METRICS → REPORT`

DAG 1.3.0 保留九个阶段。EVENTS 升级为 `event-evidence-1.0.0`，复用 ACTIVITY/RALLY 的 BlurBall 轨迹缓存，保留二维球路、视觉触球证据和可选 MediaPipe 3D 骨架。TIMELINE 将每个回合绑定到生成它的 EVENTS artifact；API 不通过“最近运行”拼接其它版本的数据。

1. 原视频、BlurBall 和姿态均使用相对原片的毫秒时钟。姿态以可配置 12 fps 采样，播放代理为 30 fps；不宣称逐源帧精确重建。
2. 每拍保留视觉支持、声音支持、原始视觉时间；置信度不使用未校准热图峰值。
3. 前端逐拍跳转、慢放、球路／骨架叠加、3D 视角旋转、并排对照共用播放器时间。
4. 用户为固定机位区间标定四角。球台平面单应矩阵只投影疑似落台点，不投影飞行中的球来伪造三维位置。
5. 连续可见轨迹的向下／向上反转、球台范围和触球间隔生成落台候选。发球到对方台面、接发到对方台面或一般落台由复核者确认后进入全片九宫格统计。
6. 时间线编辑后，旧落点确认退出统计；球台标定可保留。重新标定时清除旧确认。所有保存均检查基准版本，409 提示刷新。

## 启用

```bash
# Worker 环境；依赖及模型不会在分析时自动下载
.venv/bin/python -m pip install -e '../pingpong-training[vision,pose]'
alembic upgrade head  # 0008_replay_reviews
```

从 [Google 官方模型页](https://ai.google.dev/edge/mediapipe/solutions/vision/pose_landmarker#models) 下载 Pose Landmarker Full task：

```dotenv
SPINREAD_BLURBALL_WEIGHTS=/absolute/path/to/blurball_best
SPINREAD_POSE_WEIGHTS=/absolute/path/to/pose_landmarker_full.task
SPINREAD_POSE_FPS=12
```

重启 API/worker。新视频自动生成证据；旧视频使用现有“重新分析”，或 `scripts.relabel_video --base-version ... --apply` 复用媒体步骤。不要在后台任务未完成时盲目重启或全库重跑。未配置姿态模型时显示不可用；错误的模型路径会明确失败，不返回假骨架。

本地已安装 MediaPipe 0.10.21、下载官方 Full 模型、追加 pose 配置并执行 0008 迁移。原数据库存在 14 条遗留 RUNNING 状态记录，但进一步只读检查确认任务队列为空、无任何已领取任务；因此已优雅重启 :8000 开发 API，并验证三个新复盘接口加载成功。历史运行记录未修改。隔离验收 API 使用临时 SQLite 与本地媒体，不修改原视频／账号／分析记录。

## 接口

- `GET /api/videos/{video}/rallies/{rally}/replay?timeline_version=N&start_ms=T`：最多返回 30 秒，含 `next_start_ms`、触球、球路、姿态、当前回合落点及复核版本。
- `GET /api/videos/{video}/replay-statistics?timeline_version=N`：当前时间线全片已确认发球／接发／相持落点统计及待复核数量。
- `PUT /api/videos/{video}/replay-review`：`base_version`、`timeline_version`、四角、固定机位区间、`fixed_camera: true`、落点确认。只接受当前视觉候选，禁止重复／越界／旧版本确认。

读取继承 `viewable_video`，写入只允许所有者；删除视频或撤销授权后拒绝读取。模型本机路径不出现在响应中。保留服务器的 private/no-store 策略。新增表只保存校准和人工标签；视频依旧存放在原有私有媒体存储中。

## 范围和限制

- 自动化的是轨迹检测、触球候选、落台候选、姿态采样和数据交付；机位标定与发接发语义仍需复核。
- 骨架是单目、髋中心相对的模型估计，支持视角旋转和可见关节角度参考；不是球台坐标系中的人体网格重建。缺失关节和帧不插值。
- 近／远端按当前图像中髋部位置排序，不承诺持续身份。只检测到一人时保留位置 UNKNOWN，可在页面选择“单人／位置待确认”。遮挡多的近端可切换到远端查看。
- 不生成球拍刚体、旋转 RPM、米／秒球速、自动得失分或自动动作技术分类。
- 球台统计左右沿标定方向，长短是距球网的相对区域，不是接发判断题的第二落点；没有足够证据时为零样本，不生成成功率。
- 当前事件 artifact 与姿态缓存仍是单个 JSON；接口分页限制前端传输，但服务器读取长录像 artifact 的内存与统计耗时需要后续分块优化。应先在短片和固定机位上验收。

## 验证

- 分析内核：缺失帧、源时钟、隐私字段、编辑后的命中集合、无效四角、单应矩阵、连续轨迹支持、确认样本分母、可见关节角度。
- API：所有权／删除门槛、时间线冲突、标定版本冲突、重标定失效、未知候选拒绝、旧时间线空态。
- 前端：空序列、遮挡断线、过期帧、3D 投影、TypeScript 与生产构建；原导航测试继续通过。
- 使用 `IMG_8316.MOV` 的 12–20 秒作为私有验证片段：240 个球路输入帧、239 帧检测到球，1 个回合、15 个触球候选；96 个姿态采样帧均有至少一个人物；依据可见四角标定后产生 11 个落点候选。这是本次输出计数，未进行准确率标注评估。
- 浏览器验收：登录隔离环境、回合选择、逐拍跳转、原片／3D 同步、视角旋转、四角输入、候选落点与零已确认样本。人工确认统计另有 API 回归测试，不将测试确认伪装为真实训练标注。

本轮前端 14 项测试和生产构建通过；后端新增复盘／视觉链路 8 项回归通过，分析内核与后端原有测试套件亦通过可运行项。依赖外部 PostgreSQL／MinIO 的后端集成测试在受限测试环境跳过，未启动测试 worker 去消费开发队列。

# 球员、AI 与真人教练的训练闭环

AI 整理证据与初步建议，球员记录练习，真人教练据此指导、修订任务，再以条件一致的视频复测。系统不将录制质量、切分指标或私人 quiz 的自述答案解释为技术能力评分。

## 使用流程

1. 球员上传视频并等待当前时间线报告发布。在训练计划页确认训练场景、目标球员、机位及对手/喂球者。
2. 从报告生成最多三条有证据的规则建议，或手动填写练习内容、次数、组次、复测指标及阈值。生成不会覆盖已有任务或教练锁定内容。
3. 教练注册 COACH 账号。球员按邮箱建立关系，再显式授权该教练访问某个视频，提交评审请求。
4. 教练在自己的队列查看视频、报告、相关 quiz 记录，添加时间段批注，修订或新建训练任务。教练新建任务默认锁定；球员仍能写练习感受和完成状态。
5. 球员选择另一段已发布报告的视频提交复测。对比记录保存当时的任务版本、指标和场景快照，后续修改不改写历史结果。

## 契约与权限

- 新增写接口要求 `Idempotency-Key`，24 小时内相同账号、键、操作和请求体返回原结果；键用于不同请求返回 409。重放仍先检查当前权限。
- 修改上下文、计划、授权、评审使用 `base_version`；冲突返回 409。
- 教练访问必须同时满足 ACTIVE 教练关系和该视频的 ACTIVE COACH_VIEW consent。注册为教练不会自动获得资源。
- 撤销任一许可取消未完成评审，并阻断后续视频、媒体、报告、计划和评审访问。已发送到浏览器的数据不能远程收回；API 使用 private/no-store。
- 教练不能删除视频、代填球员感受或修改源时间线。已提交的教练反馈保留给球员。
- 复测视频也需独立授权，教练才能查看对应复测结果。
- 有审计记录的范围：本轮训练闭环写入、媒体访问、视频删除；尚未覆盖全部旧接口。

## API

- `POST /api/auth/register`、`GET /api/auth/me`
- `GET/POST /api/coach-grants`、`POST /api/coach-grants/{id}/revoke`
- `GET/POST /api/videos/{id}/consents`、`POST /api/consents/{id}/revoke`
- `GET/POST /api/review-requests`、`GET /api/review-requests/{id}`、`POST .../{id}/feedback`、`POST .../{id}/cancel`
- `GET /api/coaches/{id}/review-queue`
- `GET/PATCH /api/videos/{id}/context`、`GET /api/reports/{id}/evidence`
- `GET /api/users/{id}/training-plans/active`、`POST /api/training-plans/generate`、`POST /api/training-plans/items`
- `PATCH /api/training-plans/{plan}/items/{item}`、`GET/POST .../{item}/retests`

完整请求字段见 FastAPI `/docs` 和 `tests/test_training_loop.py`。

## 复测边界

只有不同视频、有效且当前的报告、相同 session 类型、完整且匹配的训练场景/目标球员/机位/对手、匹配的指标与质量能力版本、达到最低样本量和非零基线才能计算变化。任一条件不满足，结果保留原因，delta、relative_change、success 均为 null。场景信息是人工声明，并非视觉身份识别。当前质量能力不支持的回合指标不会因有数值而被当作可比。

迁移：`.venv/bin/python -m alembic upgrade head`。测试：`.venv/bin/python -m pytest`；隔离测试使用虚构球员/教练，集成测试结束清理上传数据。

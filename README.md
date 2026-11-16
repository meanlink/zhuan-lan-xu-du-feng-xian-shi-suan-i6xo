# 专栏续读风险试算

分类与阈值选择的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览、审计轨迹与历史回测。
三个贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划；辅助 User、Audit、Delivery。

### 历史回测案
`#/backtest` 页面把去年的读者行为与续读标签按“历次修订 + 实际接收时间”还原成多个时点，逐点生成试验，避免把今天才补齐的结果算进去年。
- **两种时间**：`occurred_at` 业务发生、`matured_at` 标签到期、`received_at` 实际接收；接收时间由服务端整批写入，接口拒绝 `received_at` 字段，不能回填或改写。
- **时点口径**：每个时点只取 `received_at <= 时点` 且 `matured_at <= 时点` 的该读者最新修订；迟到记录与更正只影响其后建立的案件/时点，不改写过去。
- **建案冻结**：接收截止时间、批次清单、各时点纳入/排除决定与快照指纹在 `POST /backtest/cases` 时固化；并发接收的批次各自整批落库，不混入半批资料。
- **逐点执行与恢复**：每时点至多一个试验（`backtest_trials.point_id` 唯一）；单点失败保留其他结果，`POST /backtest/cases/{id}/resume` 从原进度补跑，重试复用同一试验。
- **门禁**：全部时点成功才开放整案下载（JSON + 纳入清单 CSV）、负责人批准与内部简报归档；未齐全时以上接口均返回 409。
- **核对**：页面与接口给出每点的纳入人数、排除人数及原因（时点后才接收/标签未到期）、采用的修订号与接收/到期时间；`GET /backtest/revisions/{reader_id}` 查看读者历次修订。

历史回测是当时口径的还原与样本内评估，不是时间外验证，也不证明干预效果。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18207；后端文档 http://localhost:19207/docs；数据库宿主端口 17707。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、读取与下载；不能批准或安排分发|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；MySQL 8.0；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed；ReceiptStatus、CaseStatus、PointStatus、TrialStatus 见 enums。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=reader-risk；FRONTEND_PORT=18207；BACKEND_PORT=19207；DB_PORT=17707；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 同时消费分析队列与 `backtest:queue`，并按数据库未完成时点恢复回测案；worker 启动时将中断的 `running` 时点放回队列，已完成时点与试验不变。worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。历史回测演示资料由 `backend/scripts/backtest_seed_data.py` 生成：含 2025 年 10 个回执批次、10 月接收的更正批、次年 3 月接收的迟到批和今日接收的补齐批。测试与截图统一存放 ../../test-artifacts/gb-1008/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。

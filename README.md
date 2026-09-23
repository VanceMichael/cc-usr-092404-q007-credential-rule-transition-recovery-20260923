# 绿色金融能力凭证核验引擎

管理能力目录、机构凭证与岗位核验；在资格规则换版前，提供**影响演练**能力：
在固定的凭证水位上列出受影响岗位、可用替补与无法自动裁定的等效项，
让绿色项目合规负责人可以在规则切换前直接安排补员。

Litestar 提供异步 HTTP 运行环境，SQLAlchemy 与 Alembic 只访问 SQLite，
默认数据库位于 `data/skills.sqlite3`。

```bash
python -m pip install -e ".[test]"
python -m alembic upgrade head
pytest
uvicorn skill_engine:app
```

数据库位置由 `DATABASE_PATH` 覆盖，服务端口通过启动参数或 `PORT` 传入。
源码、迁移与测试相互分离，容器启动前会先执行结构升级。

## 领域概念

- **规则适用范围**：（地区, 项目类型）。每个范围维护一条唯一时间线
  （`rule_timeline_heads` 指针 + `rule_events` append-only 账本）。
- **规则草案** `rule_drafts`：声明地区、项目类型、新版本号、生效/失效区间、
  被替代版本、岗位凭证要求与过渡条款（`grandfather_policy`）。
- **发布段** `published_rules`：每一段规则带半开时间区间
  （`start_on` 起、`end_on` 止），任意日期至多一段有效。
- **演练运行** `drill_runs`：草案 + 凭证水位日 + 范围确定一次运行；
  运行级 `input_digest` 保证相同输入复用，逐项目 `fragment_digest`
  保证局部修改只重算相关项目；`frozen_inputs` 固化启动时快照，续跑不重读实时表。
- **待发箱** `notifications`：决定与时间线变更在业务事务内落库，
  独立投递器搬运，重启不丢、按幂等键不重。

## 裁定结果（逐项目）

每个项目在生效日采用哪一版规则、为何进入某个名单，都带文字原因：

| outcome | 含义 |
| --- | --- |
| `compliant` | 在适用版本（新版或过渡保留的旧版）下岗位凭证齐备 |
| `staffing` | 存在缺凭证或缺配岗位，列出同地区空闲替补，可直接补员 |
| `review` | 有 `pending` 状态的等效凭证无法自动裁定，转人工复核 |
| `exempt` | 豁免在生效日仍有效 |
| `completed_snapshot` | 项目已完工或全部岗位结论已签署，固定签署时规则快照 |

裁定要点：

- **过渡条款**：生效前已启动的项目，按 `project_start + grace_days`
  决定是否保留旧资格；宽限过期或 `mode=none` 时适用新版。
- **签署固定**：已签署结论不随换版重开；签署后发生的凭证撤销只追加
  `revocation_risks`，不改变原结论。
- **在办撤销**：在执行岗位的凭证若在水位日前撤销且造成缺口，进入补员并记风险。
- **替补池**：同地区、未在任何在办项目上承担未签署岗位的人员；
  直接持证或凭已接受等效项满足要求才入选。

## 时间线并发语义

草案维护（登记/修订/提交）只能由规则维护者进行；**批准人必须是维护者之外的用户**。
发布在单个 `BEGIN IMMEDIATE` 事务内原子完成：旧段截断到生效日前一天、
新段写入、指针移动、草案置 active、事件追加、通知入待发箱。

撤回与紧急延期与发布竞争时，全部经同一立即写事务串行，
事件账本按（范围, seq）严格递增，最终只留下一条有效时间线。

## HTTP 接口

### 演示数据 / 订阅

- `POST /admin/seed`：按主键幂等写入用户、人员、凭证、等效项、项目、
  岗位、订阅，并可引导旧条款时间线。
- `POST /subscriptions`、`GET /subscriptions`：地区/类型支持 `*` 通配。

### 规则生命周期

- `POST /rule-drafts`（维护者）
- `POST /rule-drafts/{id}/revisions`（草案态修订，版本号自增）
- `POST /rule-drafts/{id}/submit`（提交审批）
- `POST /rule-drafts/{id}/approve`（**非维护者**批准）
- `POST /rule-drafts/{id}/publish`（原子切换）
- `POST /timelines/bootstrap`：登记当前旧条款作为时间线起点
- `GET /timelines/{region}/{project_type}`：段、事件、当前指针
- `POST /rules/withdraw`、`POST /rules/emergency-extend`

### 演练

- `POST /drills`：`{draft_id, watermark_date, batch_size?}`。
  返回运行摘要；相同输入返回既有运行（`reused=true, reuse_mode=exact`），
  局部修改复用未变片段（`reuse_mode=fragment`，`copied_projects` 为重算外的复用数）。
- `POST /drills/{run_id}/resume`：从断点继续。
- `GET /drills/{run_id}/impact`：补员影响清单（受影响岗位、替补、待裁定等效项、撤销风险）。
- `GET /drills/{run_id}/projects/{project_id}`：单项目视图，含适用版本与逐条原因。

### 待发箱

- `GET /notifications?status=&recipient=`、`GET /notifications/stats`
- `POST /notifications/drain`：搬运待发通知（生产环境把投递器替换为邮件/消息网关，
  外部侧按 `idempotency_key` 幂等；失败回 pending，sending 残骸自动回收）。

## 开发检查

- 编译检查：`python3 -m compileall -q src`
- 全量测试：`pytest`（覆盖全部裁定分支、摘要复用/增量、冻结水位续跑、
  职责分离、发布/撤回/延期并发、待发箱重启与去重）

# 绿色金融能力凭证核验引擎

项目用于管理能力目录、机构凭证和岗位核验，并在资格标准换版前提供**影响演练**：在固定凭证水位上列出受影响岗位、可用替补与需人工复核的等效项，让绿色项目合规负责人在规则切换前就能安排补员。Litestar 提供 HTTP 运行环境，SQLAlchemy 与 Alembic 只访问 SQLite，默认数据库位于 `data/skills.sqlite3`。

```bash
python -m pip install -e ".[test]"
python -m alembic upgrade head
pytest
uvicorn skill_engine:app
```

数据库位置由 `DATABASE_PATH` 覆盖，服务端口通过启动参数或 `PORT` 传入。源码、迁移与测试相互分离，容器启动前会先执行结构升级。

## 换版影响演练的工作方式

1. **维护规则草案**：`POST /rules/drafts` 声明地区、项目类型、有效区间（`effective_from`/`effective_to`）、与旧条款的替代关系（`supersedes_version_id`）和过渡条款（`transition.grandfather`）。草案内容包含角色凭证要求、自动等效关系和属性豁免。
2. **职责分离批准**：`POST /rules/{id}/approve` 的批准人必须不同于草案维护人；`POST /rules/{id}/publish` 是一次原子切换——更新生效指针、收口旧条款开放区间、追加时间线事件在同一事务完成。撤回（`withdraw`）与紧急延期（`extend`）同样追加时间线；SQLite 写事务以 `BEGIN IMMEDIATE` 串行，叠加 `(scope_key, seq)` 唯一约束，竞争时只留下一条有效时间线，撤回也不会复活旧版。
3. **固定水位评估**：`POST /projects/{id}/evaluate` 按 `as_of` 截取凭证水位（水位日后签发、水位日前撤销的凭证均不计入）。结论同时说明**采用哪一版规则及原因**：
   - 已签署项目固定签署时规则快照（`signed_snapshot`）；
   - 生效日前启动的在办项目按过渡条款保留旧资格（`grandfathered`）或采用新规；
   - 岗位分类为补员 `backfill`（列出同地区可用替补）、豁免 `exempt`、复核 `review`（存在无法自动裁定的人工等效项）；
   - 凭证撤销另行追加为撤销风险，不与资格结论混同。
4. **影响清单演练**：`POST /dry-runs` 在固定水位批量裁定。`input_summary` 覆盖草案、水位与全部基础数据，相同草案重跑直接复用已完成演练；每个项目另有内容寻址缓存，规则中无关角色的改动不会改变其输入切片，因此**局部修改只重算相关项目**。
5. **断点续跑与待发箱**：`batch_size` 可把长批次拆段，`POST /dry-runs/{id}/resume` 从持久断点继续。变更决定与撤销风险通知与裁定结果同事务写入持久待发箱（`GET /notifications`、`POST /notifications/{id}/deliver`），幂等键绑定输入切片，重启不漏发、不重复打扰同一收件人。

## 开发检查

- 编译检查：`python3 -m compileall -q src`
- 全量测试：`pytest`（含真实 Alembic 迁移建库、跨引擎重启续跑与时间线并发竞争用例）

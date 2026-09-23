# 绿色金融能力凭证核验引擎

项目用于管理能力目录、机构凭证和岗位核验。Litestar 提供异步 HTTP 运行环境，SQLAlchemy 与 Alembic 只访问 SQLite，默认数据库位于 `data/skills.sqlite3`。

```bash
python -m pip install -e ".[test]"
python -m alembic upgrade head
pytest
uvicorn skill_engine:app
```

数据库位置由 `DATABASE_PATH` 覆盖，服务端口通过启动参数或 `PORT` 传入。源码、迁移与测试相互分离，容器启动前会先执行结构升级。

## 开发检查

- 编译检查：`python3 -m compileall -q src`

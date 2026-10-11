# MyGPT — 项目约定

给 Claude Code 的项目级说明。**先读这份，再动代码。**

## 1. push main = 直接生产部署（无 CI）

`.github/workflows/deploy.yml` 是仓库里唯一的部署工作流：

**push 到 `main` → 构建并推送 backend/frontend 镜像到 GHCR → 向 `deploy`
分支发部署信号**（实测全程 ~26s）。服务器上的 `mychat-deploy.timer`
每 2 分钟轮询该信号，检测到即自动执行 拉镜像 → 迁移 → 重启 → 健康检查
（失败自动回滚上一版镜像）。**push → 生产生效约 1~2 分钟。**

推一个普通 feature 分支**不会触发任何 Action**——没有 PR 发布流程，
需要隔离验证就临时自己跑测试（第 4 节）。

`deploy` 分支**不要删**——它是部署信号通道，删了生产部署管线就断了。

**没有 CI 门禁**：pytest/ruff/alembic/eval 全都不在部署前跑（已删
`ci.yml`）。所以推之前本地先跑绿（第 4 节）；带迁移的还要跑第 5 节的
脚本——服务器脚本有健康检查失败自动回滚兜底，但那是"崩了才发现"。

## 2. 提交推送后，固定动作：用 `gh` 确认 Deploy 结果

改完代码提交、推送之后，**必须用 `gh` 确认这次 push 触发的 Deploy 结果**，
不要只凭推送成功就宣布完成：

```bash
gh run list --repo qiangyidian/MyGPT --limit 5                 # 最近几次运行
gh run watch <run-id> --repo qiangyidian/MyGPT                 # 实时跟进当前这次
gh run view <run-id> --repo qiangyidian/MyGPT --log-failed     # 只看失败步骤的日志
```

## 3. 远端不叫 `origin`

`git remote -v` → 远端名是 **`MyGPT`**：

```
MyGPT  https://github.com/qiangyidian/MyGPT.git
```

所以是 `git push MyGPT <branch>`，不是 `git push origin`。默认分支 `main`。

## 4. 本地检查（唯一的防错线）

没有 CI 了，本地检查是改动上生产前的唯一防线。backend（在 `backend/` 下）：

```bash
ruff check app tests
pytest tests -q --tb=short \
  --deselect tests/test_agent_phase2.py::test_agent_mode_emits_plan_created \
  --deselect tests/test_agent_phase5.py::test_full_native_agent_path
```

- 上面两个 deselect 是网络依赖用例（会打真实模型端点），无网环境下跳过。
- `tests/test_agent_graph_lifecycle.py::test_multi_agent_approval_pauses_then_resumes`
  本地已知偶发死锁，会级联出一批 `OperationalError`，与本意改动无关；
  本地验证时可 deselect。
- `ruff format` 不在检查之列（仓库有历史格式债，只跑 `ruff check`）。
  不要顺手全量 reformat，那会把 diff 冲垮。

frontend（在 `frontend/` 下）：`npm run typecheck && npm run lint && npm run test`

## 5. 迁移：没有任何自动验证，推 main 前必须单独验

生产部署时迁移是**在生产库上真跑的**，而部署前没有一道门禁会跑 alembic。
仓库自带 `scripts/verify_migrations.sh` 补这个洞：起一个隔离的 `postgres:16`，
跑两条路径：

1. 空库 → head
2. prior revision → head —— **这条才是真实部署路径**

```bash
./scripts/verify_migrations.sh     # 需要 docker + backend venv 里的 psycopg2
```

**任何新增 `backend/migrations/versions/` 文件的改动，推 main 之前先跑这个。**

（该脚本历史上曾因 `REPO_HEAD` 引用顺序问题，在 `set -u` 下静默 SKIP 掉路径 2
却仍打 PASS——已修。改这个脚本时注意 `PRIOR_REV` 必须在 `REPO_HEAD` 之后求值。）

## 6. 文案与注释语言

本项目是 zh-CN 产品：面向用户的文案用中文；注释/文档中允许全角标点（，：），
`pyproject.toml` 里已为此关掉 `RUF001/RUF002/RUF003`，那不是笔误。

# MyGPT — 项目约定

给 Claude Code 的项目级说明。**先读这份，再动代码。**

## 1. 提交推送后，固定动作：用 `gh` 查 Action 运行状态

改完代码提交、推送之后，**必须用 `gh` 确认这次 push 触发的 Action 结果**，
不要只凭"本地测试绿了"就宣布完成。

```bash
gh run list --repo qiangyidian/MyGPT --limit 5                 # 最近几次运行
gh run watch <run-id> --repo qiangyidian/MyGPT                 # 实时跟进当前这次
gh run view <run-id> --repo qiangyidian/MyGPT --log-failed     # 只看失败步骤的日志
```

## 2. 硬约束：CI 什么时候跑、什么时候会自动部署

这两条决定"往哪推"，不要凭直觉推分支。

**① CI 只在两种情况下触发**（`.github/workflows/ci.yml:9-11`）：

- push 到 `main`
- 开 PR 到 `main`

推一个普通 feature 分支**不会触发任何 Action**。想验证改动、想看运行状态，
**必须开 PR**。

**② push 到 `main` 会自动部署生产**（`.github/workflows/deploy.yml`）：

CI 在 main 上成功后，"Deploy Signal" 往 `deploy` 分支发信号；服务器上的
`mychat-deploy.timer` 每 2 分钟轮询 `origin/main` + 该信号，检测到即自动执行
拉代码 → 构建 → 迁移 → 重启 → 健康检查（失败回滚）。

**所以不要为了"看 CI 跑没跑"往 main 推。** 走 PR；合并到 main 是一个部署动作，
需要单独决策，不要顺手做。

## 3. 远端不叫 `origin`

`git remote -v` → 远端名是 **`MyGPT`**：

```
MyGPT  https://github.com/qiangyidian/MyGPT.git
```

所以是 `git push MyGPT <branch>`，不是 `git push origin`。默认分支 `main`。

## 4. 本地跑等价于 CI 的检查

backend（在 `backend/` 下）：

```bash
ruff check app tests          # 门禁项，必须全绿
pytest tests -q --tb=short \
  --deselect tests/test_agent_phase2.py::test_agent_mode_emits_plan_created \
  --deselect tests/test_agent_phase5.py::test_full_native_agent_path
```

- `ruff` 在 CI 里**钉死**为 `ruff==0.15.17`。浮动的版本会因为上游删规则而炸掉
  lint 门禁，而**红色 CI 会阻断部署信号**。改 `pyproject.toml` 规则时同步改钉的版本。
- 上面两个 deselect 是网络依赖用例（会打真实模型端点），CI 里跳过。
- `tests/test_durable_controls.py::test_multi_agent_approval_pauses_then_resumes`
  本地已知偶发死锁，会级联出一批 `OperationalError`，与本意改动无关；
  本地验证时可 deselect。
- **`ruff format` 不在门禁里**（仓库有历史格式债，只跑 `ruff check`）。
  不要顺手全量 reformat，那会把 diff 冲垮。

frontend（在 `frontend/` 下）：`npm run typecheck && npm run lint && npm run test`

## 5. 文案与注释语言

本项目是 zh-CN 产品：面向用户的文案用中文；注释/文档中允许全角标点（，：），
`pyproject.toml` 里已为此关掉 `RUF001/RUF002/RUF003`，那不是笔误。

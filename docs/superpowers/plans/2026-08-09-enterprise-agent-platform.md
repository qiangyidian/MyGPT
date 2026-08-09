# Enterprise Agent Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade MyGPT into a durable, model-aware, policy-controlled enterprise Agent platform covering all approved P0, P1, and P2 requirements.

**Architecture:** FastAPI remains the API plane while PostgreSQL becomes the authoritative workflow/event store and Redis Streams becomes the execution and signal fabric. A separate worker executes a typed planner–executor–verifier state machine; all built-in, workspace, sandbox, browser, MCP, connector, and multimodal tools pass through one audited gateway. Next.js consumes replayable run events and first-class artifact/memory APIs.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, Alembic, PostgreSQL 16, Redis 7 Streams, Qdrant, httpx, Docker/Kubernetes-compatible runners, Next.js 14, React 18, TypeScript, Vitest, pytest.

---

## File structure

- `backend/app/model_capabilities.py`: validated model capability contract and provider option mapping.
- `backend/app/agents/token_budget.py`: prompt/output budget calculation, admission checks, and continuation overlap merge.
- `backend/app/agents/workflow/`: durable workflow state machine, plan schema, executor, verifier, repository, queue, controls, and recovery.
- `backend/app/agents/events.py`: durable sequenced run-event persistence and replay.
- `backend/app/agents/sandbox/`: runner protocol, local development runner, and isolated Docker runner.
- `backend/app/tools/workspace.py`: workspace-confined file, search, patch, shell, and Git tools.
- `backend/app/agents/mcp_transport.py`: stdio and Streamable HTTP JSON-RPC transports.
- `backend/app/connectors/`: tenant-scoped MCP connector configuration and encrypted credential handling.
- `backend/app/artifacts/`: artifact metadata, storage, authorization, and multimodal generation services.
- `backend/app/observability.py`: OpenTelemetry-compatible spans and Prometheus metrics with no-op fallbacks.
- `backend/app/worker.py`: worker process entry point.
- `backend/app/recovery.py`: lease recovery and stale-run reconciliation entry point.
- `frontend/src/lib/run-events.ts`: cursor-aware replay event contract.
- `frontend/src/hooks/useDurableAgentRun.ts`: reconnect/replay/background-run client state.
- `frontend/src/components/artifacts/`: generated artifact presentation and downloads.

### Task 1: Model capability registry and token admission

**Files:**
- Create: `backend/app/model_capabilities.py`
- Create: `backend/app/agents/token_budget.py`
- Create: `backend/tests/test_model_capabilities.py`
- Create: `backend/tests/test_token_budget.py`
- Modify: `backend/app/models/model_config.py`
- Modify: `backend/app/schemas/model_config.py`
- Modify: `backend/app/providers/base.py`
- Modify: `backend/app/providers/openai_compatible.py`
- Modify: `backend/app/services/chat_service.py`
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/app/settings/models/page.tsx`

- [ ] **Step 1: Write failing capability and budget tests**

```python
def test_prompt_budget_reserves_output_tools_and_margin():
    caps = ModelCapabilities(context_window=32768, max_output_tokens=8192)
    budget = calculate_prompt_budget(caps, requested_output=4096, tool_schema_tokens=1000)
    assert budget.input_tokens == 32768 - 4096 - 1000 - budget.safety_margin

def test_oversized_latest_turn_is_rejected_not_silently_trimmed():
    with pytest.raises(PromptAdmissionError) as exc:
        admit_latest_turn(latest_turn_tokens=9000, input_budget=8192)
    assert exc.value.code == "message_too_large"
```

- [ ] **Step 2: Run tests and verify missing contracts fail**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_model_capabilities.py tests/test_token_budget.py -q`
Expected: collection failures for missing modules.

- [ ] **Step 3: Implement validated capabilities and prompt admission**

```python
@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int
    max_output_tokens: int
    supports_tools: bool = False
    supports_parallel_tools: bool = False
    supports_vision: bool = False
    supports_audio_input: bool = False
    supports_audio_output: bool = False
    supports_image_generation: bool = False
    supports_structured_output: bool = False
    supports_reasoning_effort: bool = False

def calculate_prompt_budget(caps, requested_output, tool_schema_tokens=0, safety_ratio=0.05):
    reserve = min(max(1, requested_output), caps.max_output_tokens)
    margin = max(256, int(caps.context_window * safety_ratio))
    return TokenBudget(caps.context_window - reserve - tool_schema_tokens - margin, reserve, margin)
```

- [ ] **Step 4: Persist capability fields and map provider parameters**

Add additive model columns and translate `max_output_tokens` to the configured provider parameter (`max_tokens` by default, `max_completion_tokens` when selected). Validate positive ranges in Pydantic schemas.

- [ ] **Step 5: Integrate admission before history trimming**

Calculate tool-schema tokens, reserve output, reject an oversized newest turn with code `message_too_large`, and trim history only to the remaining input budget.

- [ ] **Step 6: Verify tests and existing chat tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_model_capabilities.py tests/test_token_budget.py tests/test_chat_stream.py tests/test_context_compaction.py -q`
Expected: all selected tests pass.

### Task 2: Automatic continuation and complete usage accounting

**Files:**
- Create: `backend/app/agents/continuation.py`
- Create: `backend/tests/test_continuation.py`
- Modify: `backend/app/services/chat_service.py`
- Modify: `backend/app/agents/runtime/native_runtime.py`
- Modify: `backend/app/agents/streaming_writer.py`
- Modify: `backend/app/providers/openai_compatible.py`
- Modify: `frontend/src/hooks/useChatStream.ts`

- [ ] **Step 1: Write failing overlap and continuation-policy tests**

```python
def test_merge_continuation_removes_repeated_overlap():
    assert merge_continuation("alpha beta gamma", "beta gamma delta") == "alpha beta gamma delta"

def test_auto_continue_is_bounded():
    policy = ContinuationPolicy(max_rounds=2)
    assert policy.should_continue("length", round_number=1)
    assert not policy.should_continue("length", round_number=2)
```

- [ ] **Step 2: Verify RED, then implement bounded continuation**

The runtime persists each continuation round, asks the model to continue without repetition, merges overlap, stops on non-length finish, and records a resumable checkpoint if the configured round limit is reached.

- [ ] **Step 3: Accumulate usage across every model round**

Replace last-chunk-only usage assignment with additive prompt/completion/cached/reasoning token accounting and compute cost from the aggregate.

- [ ] **Step 4: Run continuation, streaming, and accounting tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_continuation.py tests/test_streaming_writer.py tests/test_chat_stream.py tests/test_analytics.py -q`
Expected: all selected tests pass.

### Task 3: Wire real Agent budgets and provider controls

**Files:**
- Create: `backend/tests/test_budget_integration.py`
- Modify: `backend/app/agents/policies/budget_policy.py`
- Modify: `backend/app/agents/runtime/native_runtime.py`
- Modify: `backend/app/agents/runtime/crewai_runtime.py`
- Modify: `backend/app/agents/runtime/stage_executor.py`
- Modify: `backend/app/core/config.py`

- [ ] **Step 1: Write failing settings, token, cost, timeout, and replan budget tests**

```python
def test_budget_limits_are_built_from_settings(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_STEPS", "3")
    assert BudgetLimits.from_settings(Settings()).max_agent_steps == 3

def test_usage_consumes_token_and_cost_budget():
    guard = BudgetGuard(BudgetLimits(max_total_tokens=10, max_cost_usd=1))
    guard.add_usage(total_tokens=10, cost_usd=0.1)
    with pytest.raises(BudgetExceeded):
        guard.check()
```

- [ ] **Step 2: Verify RED and implement `from_settings` plus real usage**

Add `max_cost_usd`, serialize complete snapshots, and invoke gates before and after every model/tool/step/replan operation.

- [ ] **Step 3: Wrap every CrewAI stage in the remaining wall-clock timeout**

Use `asyncio.timeout(guard.remaining_seconds)` and record stage usage through the shared guard.

- [ ] **Step 4: Run budget and runtime tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_budget_integration.py tests/test_agent_phase0.py tests/test_agent_graph_lifecycle.py -q`
Expected: all selected tests pass.

### Task 4: Durable workflow schema, event store, leases, and controls

**Files:**
- Create: `backend/migrations/versions/0006_enterprise_workflow.py`
- Create: `backend/app/models/run_event.py`
- Create: `backend/app/models/run_lease.py`
- Create: `backend/app/models/run_command.py`
- Create: `backend/app/models/agent_attempt.py`
- Create: `backend/app/agents/events.py`
- Create: `backend/app/agents/workflow/repository.py`
- Create: `backend/app/agents/workflow/controls.py`
- Create: `backend/tests/test_durable_events.py`
- Create: `backend/tests/test_durable_controls.py`
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/app/api/agent_runs.py`

- [ ] **Step 1: Write failing monotonic event and exactly-once command tests**

```python
async def test_run_events_get_monotonic_sequences(db_session, run):
    first = await store.append(run.id, "run.started", {})
    second = await store.append(run.id, "step.started", {})
    assert (first.sequence, second.sequence) == (1, 2)

async def test_instruction_is_claimed_once(db_session, run):
    command = await commands.append(run.id, "instruction", {"text": "check sources"})
    assert await commands.claim_pending(run.id) == [command]
    assert await commands.claim_pending(run.id) == []
```

- [ ] **Step 2: Verify RED and add additive workflow tables**

Use a unique `(run_id, sequence)` constraint for events, command status transitions, and lease owner/expiry/version fields for optimistic fencing.

- [ ] **Step 3: Implement transactional repositories**

Event sequence allocation and step transitions execute in one database transaction. Commands move `pending -> claimed -> applied` and remain auditable.

- [ ] **Step 4: Replace in-memory API controls with durable commands**

Pause, resume, cancel, instruction, approve, and reject persist first and publish second. Database state remains correct when Redis is unavailable.

- [ ] **Step 5: Run migration and repository tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_durable_events.py tests/test_durable_controls.py tests/test_agent_phase3.py -q`
Expected: all selected tests pass.

### Task 5: Redis Streams queue, worker, recovery scheduler, and SSE replay

**Files:**
- Create: `backend/app/agents/workflow/queue.py`
- Create: `backend/app/agents/workflow/worker.py`
- Create: `backend/app/agents/workflow/recovery.py`
- Create: `backend/app/worker.py`
- Create: `backend/app/recovery.py`
- Create: `backend/tests/test_workflow_queue.py`
- Create: `backend/tests/test_run_recovery.py`
- Create: `backend/tests/test_event_replay.py`
- Modify: `backend/app/api/chat.py`
- Modify: `backend/app/api/agent_runs.py`
- Modify: `backend/app/services/chat_service.py`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Write failing queue idempotency, lease fencing, recovery, and replay tests**

```python
async def test_enqueue_same_run_is_idempotent(queue, run_id):
    await queue.enqueue(run_id)
    await queue.enqueue(run_id)
    assert await queue.pending_ids() == [run_id]

async def test_expired_lease_is_requeued_once(recovery, expired_run):
    assert await recovery.scan() == [expired_run.id]
    assert await recovery.scan() == []
```

- [ ] **Step 2: Verify RED and implement queue interfaces**

Provide Redis Streams production transport and deterministic in-memory test transport. Use consumer groups, explicit acknowledgement, idempotent run keys, and database lease fencing.

- [ ] **Step 3: Implement API enqueue and worker execution**

The API persists a run and returns/streams its durable events. The worker claims a lease, executes, checkpoints, renews, and acknowledges only after a terminal or safely persisted retry state.

- [ ] **Step 4: Implement stale-run recovery**

On startup and on a schedule, reconcile legacy `running` rows, expire dead leases, requeue retryable runs, and terminally fail exhausted runs with an explicit recovery reason.

- [ ] **Step 5: Implement cursor replay SSE**

Replay events after `Last-Event-ID`, then follow Redis notification or database polling. Client disconnect closes only the subscription, never the workflow.

- [ ] **Step 6: Add worker and recovery services to Compose**

Use separate commands, health checks, graceful stop periods, and no development reload in production targets.

- [ ] **Step 7: Run workflow durability tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_workflow_queue.py tests/test_run_recovery.py tests/test_event_replay.py -q`
Expected: all selected tests pass.

### Task 6: Planner–executor–verifier state machine

**Files:**
- Create: `backend/app/agents/workflow/schemas.py`
- Create: `backend/app/agents/workflow/planner.py`
- Create: `backend/app/agents/workflow/executor.py`
- Create: `backend/app/agents/workflow/verifier.py`
- Create: `backend/app/agents/workflow/engine.py`
- Create: `backend/tests/test_workflow_engine.py`
- Create: `backend/tests/test_workflow_replan.py`
- Modify: `backend/app/agents/orchestrator.py`
- Modify: `backend/app/models/agent_run.py`

- [ ] **Step 1: Write failing dependency, parallelism, retry, replan, and verification tests**

```python
async def test_independent_ready_steps_run_in_parallel(engine, plan):
    result = await engine.run(plan.with_parallel_steps("research_a", "research_b"))
    assert result.max_concurrency == 2

async def test_failed_verification_replans_within_budget(engine, plan):
    result = await engine.run(plan, verifier_results=["revise", "pass"])
    assert result.replans == 1
    assert result.status == "completed"
```

- [ ] **Step 2: Verify RED and implement strict plan schemas**

Each step includes dependencies, role/model, tool allowlist, timeout, retry policy, acceptance criteria, and cost estimate. Plans reject cycles and missing dependencies.

- [ ] **Step 3: Implement ready-set execution and checkpointing**

Execute bounded independent steps concurrently, persist attempt transitions, retry only classified transient errors, and checkpoint observations before downstream work.

- [ ] **Step 4: Implement structured verification and bounded replanning**

Verifier returns `pass`, `revise`, or `fail` with findings. `revise` consumes replan budget and produces a versioned plan retaining completed valid work.

- [ ] **Step 5: Express research, parallel research, and debate as templates**

Keep CrewAI behind a stage adapter but route new expert tasks through the durable engine.

- [ ] **Step 6: Run workflow tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_workflow_engine.py tests/test_workflow_replan.py tests/test_debate.py tests/test_agent_graph_lifecycle.py -q`
Expected: all selected tests pass.

### Task 7: Integrated compaction, output spill, and long-term memory

**Files:**
- Create: `backend/app/agents/context_manager.py`
- Create: `backend/app/agents/memory_service.py`
- Create: `backend/tests/test_context_manager.py`
- Create: `backend/tests/test_long_term_memory.py`
- Modify: `backend/app/agents/context_compaction.py`
- Modify: `backend/app/agents/output_spill.py`
- Modify: `backend/app/agents/model_switch.py`
- Modify: `backend/app/services/chat_service.py`
- Modify: `backend/app/api/memories.py`

- [ ] **Step 1: Write failing budget partition, tool-pair retention, mid-run compaction, spill, and memory-consent tests**

```python
def test_compaction_keeps_tool_call_and_result_together():
    compacted = manager.compact(history_with_tool_pair, input_budget=1000)
    assert has_complete_tool_pair(compacted)

async def test_memory_candidate_is_inactive_without_opt_in(service, user):
    memory = await service.propose(user, "prefers concise answers")
    assert not memory.active
```

- [ ] **Step 2: Verify RED and implement one context manager used by chat and workflow**

Partition budgets, preserve protected fragments, run between steps, compact on model downshift, and replace large tool results with authorized artifact handles.

- [ ] **Step 3: Implement opt-in semantic user memory**

Extract candidates, score confidence, deduplicate, expire, activate under user policy, embed active memories in Qdrant, and expose edit/delete/disable APIs.

- [ ] **Step 4: Remove process-local world-state correctness dependency**

Persist fragment fingerprints and always assemble a complete effective system prompt for each provider request.

- [ ] **Step 5: Run context and memory tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_context_manager.py tests/test_long_term_memory.py tests/test_context_compaction.py tests/test_model_switch.py tests/test_output_spill.py -q`
Expected: all selected tests pass.

### Task 8: Workspace tools and isolated runner

**Files:**
- Create: `backend/app/agents/sandbox/base.py`
- Create: `backend/app/agents/sandbox/local.py`
- Create: `backend/app/agents/sandbox/docker.py`
- Create: `backend/app/tools/workspace.py`
- Create: `backend/tests/test_workspace_tools.py`
- Create: `backend/tests/test_docker_runner_policy.py`
- Modify: `backend/app/tools/registry_init.py`
- Modify: `backend/app/agents/permission_profiles.py`
- Modify: `backend/app/core/config.py`

- [ ] **Step 1: Write failing path escape, command policy, timeout, output limit, and atomic patch tests**

```python
async def test_read_file_rejects_workspace_escape(tool, workspace):
    with pytest.raises(ToolError):
        await tool.run(path="../secret")

def test_docker_runner_has_enterprise_isolation_flags(command):
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
```

- [ ] **Step 2: Verify RED and implement canonical workspace confinement**

Provide list, search, read, atomic write, apply patch, Git status/diff, and non-interactive shell through a common runner. Resolve every path and require it to remain below the assigned workspace root.

- [ ] **Step 3: Implement production Docker isolation**

Run as non-root with read-only root, dropped capabilities, no-new-privileges, bounded CPU/memory/PIDs/time/output, explicit workspace mount, and default-deny network. Local runner refuses non-development environments.

- [ ] **Step 4: Register tools with precise risk and approval policies**

Read operations are low risk; writes, patch, shell, and Git mutations require the configured approval profile. Every operation records checksums and accepted-line counts.

- [ ] **Step 5: Run sandbox and tool tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_workspace_tools.py tests/test_docker_runner_policy.py tests/test_apply_patch.py tests/test_exec_policy.py tests/test_permission_profiles.py -q`
Expected: all selected tests pass.

### Task 9: MCP transports and connector registry

**Files:**
- Create: `backend/app/agents/mcp_transport.py`
- Create: `backend/app/connectors/models.py`
- Create: `backend/app/connectors/service.py`
- Create: `backend/app/api/connectors.py`
- Create: `backend/tests/test_mcp_stdio_transport.py`
- Create: `backend/tests/test_mcp_http_transport.py`
- Create: `backend/tests/test_connectors.py`
- Modify: `backend/app/agents/mcp_client.py`
- Modify: `backend/app/agents/mcp_catalog.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing JSON-RPC initialize/list/call/cancel tests against local fake servers**

```python
async def test_stdio_mcp_discovers_and_calls_tool(fake_stdio_server):
    client = McpSession(fake_stdio_server.config)
    tools = await client.list_tools()
    assert tools[0].name == "echo"
    assert await client.call_tool("echo", {"text": "ok"}) == {"text": "ok"}
```

- [ ] **Step 2: Verify RED and implement stdio and Streamable HTTP transports**

Implement JSON-RPC identifiers, initialize negotiation, tools/list, tools/call, cancellation, timeout, reconnect, stderr capture limits, and graceful shutdown.

- [ ] **Step 3: Route discovered MCP tools through ToolGateway**

Namespace names, preserve server provenance, validate JSON schemas, and apply the same approvals, network policy, budgets, spill, and audit as built-ins.

- [ ] **Step 4: Implement encrypted tenant connector definitions**

Provide catalog entries for GitHub, Gmail/Outlook, Google/Outlook Calendar, Slack/Teams, Notion, Drive/SharePoint/Box, Atlassian, and Figma as MCP server manifests. Store credentials encrypted and require minimum OAuth scopes.

- [ ] **Step 5: Run MCP and connector tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_mcp_stdio_transport.py tests/test_mcp_http_transport.py tests/test_connectors.py tests/test_mcp_catalog.py -q`
Expected: all selected tests pass without external credentials.

### Task 10: First-class artifacts and multimodal providers

**Files:**
- Create: `backend/app/models/artifact.py`
- Create: `backend/app/artifacts/service.py`
- Create: `backend/app/api/artifacts.py`
- Create: `backend/app/providers/multimodal.py`
- Create: `backend/tests/test_artifacts.py`
- Create: `backend/tests/test_multimodal_routing.py`
- Modify: `backend/app/schemas/chat.py`
- Modify: `backend/app/providers/openai_compatible.py`
- Modify: `backend/app/services/attachment_service.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing authorization, checksum, retention, and modality-routing tests**

```python
async def test_artifact_download_is_tenant_scoped(service, artifact, other_user):
    with pytest.raises(AppException) as exc:
        await service.open(artifact.id, other_user)
    assert exc.value.status_code == 404

def test_audio_request_rejects_text_only_model():
    with pytest.raises(ModelCapabilityError):
        route_multimodal([AudioPart(...)], text_only_caps)
```

- [ ] **Step 2: Verify RED and implement artifact metadata/storage**

Persist owner, run, step, checksum, media type, size, storage key, provenance, and retention. Use opaque download authorization and never expose local paths.

- [ ] **Step 3: Implement typed message parts and provider routes**

Support text, image, audio, and file parts. Add OpenAI-compatible transcription, speech, image generation, and image edit operations behind capability checks.

- [ ] **Step 4: Turn spills and generated files into artifacts**

Tool outputs, code bundles, screenshots, audio, images, Office documents, and PDFs use the same artifact service.

- [ ] **Step 5: Run artifact and multimodal tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_artifacts.py tests/test_multimodal_routing.py tests/test_attachment_multimodal.py -q`
Expected: all selected tests pass.

### Task 11: Observability, quotas, readiness, and evaluation gates

**Files:**
- Create: `backend/app/observability.py`
- Create: `backend/app/quotas.py`
- Create: `backend/app/evals/runner.py`
- Create: `backend/tests/test_observability_redaction.py`
- Create: `backend/tests/test_quotas.py`
- Create: `backend/tests/test_readiness.py`
- Modify: `backend/app/core/health.py`
- Modify: `backend/app/core/logging.py`
- Modify: `backend/app/main.py`
- Modify: `backend/requirements.txt`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Write failing secret-redaction, quota, and dependency-readiness tests**

```python
def test_trace_attributes_redact_api_keys():
    assert "secret" not in sanitize_attributes({"api_key": "secret"}).values()

async def test_tenant_token_quota_blocks_new_run(quota_service, exhausted_tenant):
    with pytest.raises(QuotaExceeded):
        await quota_service.admit_run(exhausted_tenant)
```

- [ ] **Step 2: Verify RED and implement optional OpenTelemetry/Prometheus adapters**

Instrumentation has no-op fallbacks when exporters are absent. Trace model calls, tools, retrieval, workflow transitions, queue latency, and artifacts with redacted attributes.

- [ ] **Step 3: Implement concurrent-run, token, cost, storage, and tool quotas**

Admission and post-usage accounting use transactional database counters and Redis rate limits without trusting client values.

- [ ] **Step 4: Add strict readiness and evaluation commands**

Readiness validates migration head, Redis, Qdrant compatibility, storage, runner, and at least one eligible chat model. Offline evaluations test deterministic contracts; live golden tasks require explicit credentials.

- [ ] **Step 5: Run observability and readiness tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_observability_redaction.py tests/test_quotas.py tests/test_readiness.py -q`
Expected: all selected tests pass.

### Task 12: Frontend durable runs, plans, memories, connectors, artifacts, and multimodal composer

**Files:**
- Create: `frontend/src/lib/run-events.ts`
- Create: `frontend/src/hooks/useDurableAgentRun.ts`
- Create: `frontend/src/components/artifacts/artifact-card.tsx`
- Create: `frontend/src/components/agents/plan-review.tsx`
- Create: `frontend/src/app/settings/connectors/page.tsx`
- Create: `frontend/src/app/settings/memory/page.tsx`
- Create: `frontend/src/lib/__tests__/run-events.test.ts`
- Create: `frontend/src/lib/__tests__/artifact-types.test.ts`
- Modify: `frontend/src/hooks/useChatStream.ts`
- Modify: `frontend/src/components/composer.tsx`
- Modify: `frontend/src/components/message-bubble.tsx`
- Modify: `frontend/src/components/context/execution-tab.tsx`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/types.ts`

- [ ] **Step 1: Write failing cursor replay, event dedupe, background run, and artifact rendering tests**

```typescript
it("deduplicates replayed events by run and sequence", () => {
  expect(reduceEvents([event(1), event(2), event(2)])).toHaveLength(2);
});

it("keeps a run active after the chat SSE subscription closes", () => {
  expect(disconnectSubscription(runningState).runStatus).toBe("running");
});
```

- [ ] **Step 2: Verify RED and implement durable run subscription**

Persist the last event cursor, reconnect with backoff, replay, deduplicate, and separate subscription state from workflow state.

- [ ] **Step 3: Add plan review and complete controls**

Render acceptance criteria, approve/revise plan, pause/resume/cancel, append instructions, and show blocked/budget/recovery states.

- [ ] **Step 4: Add memory, connector, artifact, and multimodal surfaces**

Users can enable/edit/delete memories, configure connectors, download authorized artifacts, attach audio/images/files, and select only models capable of the requested modality.

- [ ] **Step 5: Run frontend tests and type checking**

Run: `cd frontend && npm test -- --run && npm run typecheck`
Expected: all tests and type checking pass.

### Task 13: Production deployment, version alignment, migrations, backup, and recovery

**Files:**
- Create: `docker-compose.prod.yml`
- Create: `deploy/k8s/api.yaml`
- Create: `deploy/k8s/worker.yaml`
- Create: `deploy/k8s/recovery.yaml`
- Create: `deploy/k8s/sandbox-runner.yaml`
- Create: `deploy/k8s/network-policies.yaml`
- Create: `deploy/k8s/config.yaml`
- Create: `scripts/verify_migrations.ps1`
- Create: `scripts/restore-drill.ps1`
- Modify: `docker-compose.yml`
- Modify: `backend/requirements.txt`
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Pin Qdrant client compatible with the selected server**

Use a client/server pair within the supported compatibility window and add a readiness assertion so drift cannot recur silently.

- [ ] **Step 2: Make migration head mandatory in production**

Run Alembic migrations in a one-shot deployment step. API and workers refuse readiness when the database revision differs from the repository head.

- [ ] **Step 3: Add production Compose and Kubernetes resources**

Production services have no source bind mounts or reload flags, run as non-root, use health/readiness probes, resource limits, disruption budgets, default-deny network policy, and external secret references.

- [ ] **Step 4: Add backup and restore drill scripts**

Back up PostgreSQL, Qdrant snapshots, and object storage manifests; restore into isolated targets and verify checksums and migration revision.

- [ ] **Step 5: Validate manifests and migrations**

Run: `docker compose -f docker-compose.prod.yml config`
Expected: valid normalized Compose configuration.

Run: `cd backend && .venv/Scripts/alembic upgrade head && .venv/Scripts/alembic current`
Expected: current revision equals repository head.

### Task 14: Full regression, security, build, and acceptance verification

**Files:**
- Modify only files required to fix failures uncovered by the commands below, always with a failing regression test first.

- [ ] **Step 1: Run complete backend suite**

Run: `cd backend && .venv/Scripts/python -m pytest -q`
Expected: zero failures and zero version-compatibility warnings.

- [ ] **Step 2: Run complete frontend suite and production build**

Run: `cd frontend && npm test -- --run && npm run typecheck && npm run build`
Expected: zero failures and a successful production build.

- [ ] **Step 3: Run migration-from-empty and migration-from-current checks**

Run the migration verification script against isolated databases and verify both reach the same schema head.

- [ ] **Step 4: Run offline durability acceptance scenarios**

Exercise disconnect/replay, API restart, worker restart, lease expiry, duplicate delivery, approval, cancellation, budget exhaustion, automatic continuation, MCP stdio/HTTP, sandbox confinement, memory consent, artifact authorization, and modality routing.

- [ ] **Step 5: Inspect repository state and commit the verified implementation**

Run: `git diff --check && git status --short`
Expected: no whitespace errors; only intentional implementation changes remain before the final commit.


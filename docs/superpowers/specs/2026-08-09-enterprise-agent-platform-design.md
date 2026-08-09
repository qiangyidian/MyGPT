# Enterprise Agent Platform Design

## Status and decision

This specification is approved for implementation on the current `main` branch. The user has authorized autonomous engineering decisions and requested one continuous delivery covering the full P0, P1, and P2 scope.

The selected architecture is a Kubernetes-ready distributed modular monolith that preserves Docker Compose for local development. PostgreSQL is the system of record, Redis provides queues, leases, cancellation signals, and event fan-out, Qdrant provides semantic retrieval, and isolated runner containers execute untrusted code and workspace operations. The existing FastAPI and Next.js applications remain the API and product surfaces.

## Considered approaches

### Extend the current in-process runtime

This is the smallest change, but process restarts, browser disconnects, multi-worker deployments, and long-running tasks would remain unreliable. It cannot meet the requested enterprise durability requirements.

### Adopt an external workflow engine immediately

Temporal or a similar engine provides mature durable workflows, but adds a large operational dependency and would require replacing most current lifecycle code at once. It is appropriate at very large scale, but unnecessarily disruptive for the current repository.

### Selected: durable database workflow with Redis execution fabric

The current AgentRun, AgentStep, approval, audit, and SSE structures are retained. Durable workflow state and checkpoints live in PostgreSQL. Redis carries jobs, leases, controls, and event notifications; database polling remains a correctness fallback. The design exposes a workflow backend interface so Temporal can replace the Redis implementation later without changing chat or tool APIs.

## Goals

- Support model-aware long conversations and long outputs without silent truncation.
- Execute multi-step tasks independently of an open browser connection.
- Resume safely after worker crashes, application restarts, and transient provider failures.
- Dynamically plan, execute, inspect, replan, verify, and synthesize arbitrary tasks.
- Provide auditable, policy-controlled tools for files, shell, patches, Git, browser, databases, web, and MCP connectors.
- Provide user-controlled long-term memory, semantic retrieval, multimodal input/output, and artifact generation.
- Enforce tenant isolation, budgets, approvals, network policy, sandboxing, rate limits, and secret boundaries.
- Ship production deployment, migration, observability, evaluation, and disaster-recovery mechanisms.

## Non-goals

- Reimplement foundation models.
- Claim unlimited context or unlimited output; every provider has physical limits.
- Bundle third-party credentials. Integrations are complete but remain disabled until an administrator supplies credentials.
- Allow unsandboxed production code execution.

## System architecture

### API plane

FastAPI authenticates users, validates requests, persists conversations and runs, enqueues work, exposes control endpoints, and streams durable events. API processes never own the authoritative execution task. A disconnected SSE client can reconnect with an event cursor without cancelling the run.

### Worker plane

Workers claim queued runs using Redis leases backed by PostgreSQL ownership records. A worker renews its lease, checkpoints after every state transition, and releases or expires the lease on exit. A recovery scheduler requeues expired runnable work and finalizes abandoned runs whose retry policy is exhausted.

### Execution plane

The workflow engine executes a typed state machine:

1. normalize request and resolve model capabilities;
2. assemble context within a calculated token budget;
3. create or load a structured plan;
4. execute ready steps, with bounded parallelism;
5. inspect observations and update world state;
6. replan when evidence or errors invalidate the plan;
7. verify deliverables against explicit acceptance criteria;
8. synthesize and stream the final answer;
9. checkpoint usage, artifacts, memories, and terminal status.

### Tool plane

All tool calls pass through one gateway that applies tenant ownership, capability policy, argument validation, approval, network policy, timeout, output spill, audit, and idempotency. Built-in and MCP tools share the same contract. Dangerous operations use exact argument hashes, expiring approvals, and a sandbox profile.

### Data plane

- PostgreSQL: users, conversations, messages, memories, runs, plans, steps, attempts, events, leases, approvals, artifacts, usage, connector metadata, and audit records.
- Redis: durable queue transport, cancellation/pause signals, distributed locks, short-lived event fan-out, rate limiting, and cache.
- Qdrant: knowledge-base chunks, attachment chunks, conversation memory embeddings, and artifact indexes.
- Object storage: uploaded inputs, generated artifacts, spilled tool outputs, screenshots, audio, images, and exports. Local storage remains a development backend; S3-compatible storage is required in production.

## Model capability and token governance

Every model configuration has validated capabilities: context window, maximum input, maximum output, tool calling, parallel tool calling, vision, audio input/output, image generation, structured output, reasoning controls, streaming, and provider-specific parameter mapping.

The effective prompt budget is calculated as:

`input_budget = context_window - reserved_output - tool_schema_budget - safety_margin`

Requests exceeding the budget are rejected with an actionable error or compacted before dispatch. The newest user turn is never silently truncated. Attachments are chunked and retrieved when they cannot fit inline. Output truncation creates a continuation checkpoint; automatic continuation is bounded, overlap-checked, and merged without repeating prior text.

Provider adapters translate generic options to endpoint-specific parameters such as `max_tokens` or `max_completion_tokens`. Capability discovery supports administrator-supplied manifests and safe probing. Unknown capabilities fail conservatively.

## Context and memory

Context assembly assigns separate budgets to system instructions, project instructions, skills, user memory, conversation summary, recent verbatim turns, RAG evidence, tool schemas, and tool results. Compaction preserves system messages, tool-call pairs, citations, plan state, accepted facts, and a verbatim recent tail.

Compaction runs before a turn, between Agent steps, after large tool observations, and before a model downshift. Summaries include provenance and version metadata. Large outputs are stored as artifacts and represented in context by head/tail previews plus stable artifact handles.

Long-term memory is opt-in and user-manageable. Candidate preferences and facts are extracted, deduplicated, confidence-scored, and require policy approval before becoming active. Memories can expire, be edited, be deleted, and are retrieved semantically across conversations within the same tenant.

## Dynamic Agent workflow

Plans contain typed steps with dependencies, tools, assigned model or role, retry policy, timeout, acceptance criteria, and estimated cost. Ready independent steps may execute concurrently within configured limits. Roles can use different model configurations.

The planner is followed by an executor and a verifier. The verifier receives deliverables and evidence, not hidden reasoning, and returns pass, revise, or fail with structured findings. Revise triggers a bounded replan. Every loop consumes real token, time, step, tool, and cost budgets. Budget exhaustion produces a resumable terminal state rather than an ambiguous completion.

Research, parallel research, and debate become plan templates instead of special isolated runtimes. Existing CrewAI support remains available behind an adapter, while the native durable workflow is authoritative.

## Durable controls and event streaming

Pause, resume, cancel, append-instruction, approve, and reject commands are persisted and published through Redis. Workers check controls before model calls, between streamed chunks, before tools, and between steps. Instructions have monotonic sequence numbers and are consumed exactly once.

Run events are stored with an increasing per-run sequence. SSE accepts `Last-Event-ID` or an explicit cursor, replays stored events, and then follows live Redis notifications. Event retention is configurable; terminal summaries remain permanently attached to the run.

## Enterprise tool and sandbox model

Workspace tools include bounded file listing, text search, file reads, atomic writes, patch application, Git inspection, Git diff, non-interactive Git operations, shell commands, and artifact export. Browser tools support navigation, DOM inspection, screenshots, downloads, and network diagnostics.

Production execution occurs in ephemeral containers with a read-only base image, non-root user, CPU/memory/PID/time limits, workspace-only mounts, disabled host socket access, explicit environment allowlists, and default-deny network egress. Images are selected from an administrator allowlist and scanned before use. Development may use a local runner only when the environment is explicitly marked development.

## MCP and connectors

The MCP subsystem implements stdio, Streamable HTTP, and SSE transports; lifecycle, capability discovery, namespacing, invocation, cancellation, timeout, and reconnect; and conversion into the common tool gateway. Server definitions and encrypted secrets are tenant-scoped.

Connector adapters cover GitHub, email, calendars, Slack/Teams, Notion, Drive/SharePoint/Box, Atlassian, and Figma through the MCP contract. OAuth tokens are encrypted, scopes are minimized, refreshes are audited, and write actions require policy evaluation and, by default, user approval. A connector can be installed without being enabled for every tenant.

## Multimodal and artifacts

The message contract supports typed text, image, audio, and file parts. Model routing rejects unsupported modalities or selects an eligible configured model according to tenant policy. Vision preprocessing preserves originals while generating bounded derivatives. Audio supports transcription and optional streamed synthesis. Image generation and editing are provider capabilities exposed as tools.

Generated documents, spreadsheets, presentations, PDFs, images, audio, code bundles, and browser downloads are first-class artifacts with checksums, media type, size, owner, retention policy, provenance, and download authorization.

## Security and tenancy

- All data access includes tenant and user ownership checks.
- Secrets are encrypted at rest and never placed in prompts, logs, events, or tool previews.
- Tool arguments and results are redacted before observability export.
- Network access is default-deny in production with DNS rebinding and private-address protection.
- SQL tools use a read-only database role and statement timeout.
- Artifact paths are canonicalized and confined to the workspace or object-store key prefix.
- Audit events are append-only and include actor, policy decision, resource, correlation identifiers, and integrity metadata.
- Per-user and per-tenant quotas cover concurrent runs, tokens, cost, storage, connectors, and tool execution.

## Reliability and failure handling

Provider calls use bounded retries with jitter for explicitly transient failures, circuit breakers, concurrency limits, and optional model fallback policies. Tool calls have idempotency keys and attempt records. Steps distinguish retryable, blocked, cancelled, budget-exhausted, and terminal failures.

Startup runs migration checks and refuses production startup when the schema is behind. Health endpoints distinguish liveness from dependency readiness. Version compatibility checks cover PostgreSQL extensions, Redis, Qdrant server/client, sandbox runner, and configured providers.

## Observability and evaluation

OpenTelemetry traces cover API request, run, step, model call, tool call, queue wait, retrieval, and artifact operations. Prometheus metrics cover latency, first-token latency, completion status, retries, stale leases, token/cost, tool errors, queue depth, and retrieval quality. Structured logs include correlation identifiers and exclude secrets.

Evaluation suites contain deterministic unit tests, database and Redis integration tests, provider-contract tests, sandbox escape tests, reconnect/replay tests, recovery tests, RAG relevance tests, citation grounding tests, and configurable live-model golden tasks. CI blocks merge on unit, type, migration, security, and production-build failures; live tests run only when credentials are explicitly supplied.

## Deployment and operations

Docker Compose supplies PostgreSQL, Redis, Qdrant, object storage, API, worker, recovery scheduler, sandbox runner, and frontend for local and staging use. Production manifests use Kubernetes Deployments for stateless services, Jobs or isolated pods for runners, PodDisruptionBudgets, NetworkPolicies, Secrets, autoscaling, and persistent managed data services.

Backups include PostgreSQL point-in-time recovery, object storage versioning, Qdrant snapshots, and documented restore drills. Schema changes use forward-compatible expand/migrate/contract procedures. Run workers support graceful shutdown by checkpointing and releasing leases.

## Compatibility and migration

Existing conversations, messages, model configurations, Agent runs, approvals, and frontend SSE handlers remain valid. New fields are additive. Existing `running` rows are reconciled during migration. The current CrewAI runtime can execute through the new worker adapter until equivalent plan templates pass the same contract tests.

## Acceptance criteria

- A 32K model never receives an input that leaves less than the configured output reserve.
- Oversized single messages receive a deterministic validation response instead of an upstream overflow.
- A length-truncated answer can continue automatically without duplicated overlap.
- A run survives API restart, worker restart, and SSE disconnect and can replay its event stream.
- Expired leases are recovered without executing an idempotent tool twice.
- Token, time, tool, step, replan, and monetary budgets reflect actual usage and stop execution.
- Dynamic plans execute dependencies correctly, retry transient failures, replan when allowed, and verify deliverables.
- Production code execution cannot access host files, host sockets, private networks, or unapproved secrets.
- MCP tools are discovered and invoked through at least one stdio and one HTTP integration test server.
- User memory is opt-in, tenant-isolated, editable, deletable, and semantically retrieved.
- Vision, audio, and image-generation requests route only to capable models.
- Every generated artifact is authorized, checksummed, attributable, and recoverable from storage.
- Migrations reach the repository head from an empty database and the current development database.
- Unit tests, frontend tests, type checking, production builds, migration checks, and offline integration tests pass without warnings caused by version drift.


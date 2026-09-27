"""``StaleJobSweeper`` 的 leader 门禁：整个部署只有一个副本会把过期任务打回队列。

多副本部署里每个 API 副本都会起一份 sweeper（``app/main.py`` 的 lifespan），于是同一
个周期里同一批过期附件被 N 个进程各自翻回 ``pending``、各自重新派发解析一遍。那条
``UPDATE ... WHERE`` 是幂等的，所以这里从来不是"丢数据"，而是：白烧 N 倍 CPU 与解析
调用、Postgres 上 N 批 UPDATE 互相死锁、以及运维排查时看到 N 份互相矛盾的"谁在什么
时候把任务打回队列"。现在它与 retention/recovery 同源（``app.core.leader``），只有拿
到 ``stale-jobs`` 这把 advisory lock 的副本会扫。

两条主用例互为对照 —— 同一种子行在两例里都可被扫到，所以"行没变"只可能来自门禁，
而不是种子查不出来：

1. 抢不到主锁 → 一次 sweep body 都不进，行保持 ``parsing``，一条重派都没有；
2. 没有 advisory lock 可用（SQLite 测试库 / 单进程 dev）→ 照旧运行，本地与测试
   不许静默失去这条恢复路径的覆盖。

另三条钉住配套的边界：

3. ``leader_name=None`` 这个逃生口仍然每副本无条件扫；
4. 新锁名 ``stale-jobs`` 不与任何既有锁名同键（retention / recovery / 迁移屏障）；
5. 回落必须**持续** —— 门禁连着问几个 tick 都得放行，否则这个循环在 dev 与内存
   SQLite 里只跑第一轮就永久静默（``test_degraded_gate_still_leads_on_later_ticks``）。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.core.leader import _lock_key
from app.models import ChatAttachment, Conversation
from app.services import stale_job_recovery
from app.services.stale_job_recovery import StaleJobSweeper
from tests.conftest import TestSessionLocal

_SEEDED = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _stale_parsing_attachment(db_session) -> ChatAttachment:
    """一条"上一个进程死在解析中途"的附件：``parsing`` + 创建时间早于阈值。

    ``parse_status`` 必须是 ``parsing`` 而不是 ``pending``：sweep 命中后只会把
    ``parsing`` 翻成 ``pending``（见 ``_stale_attachments``），所以"仍然是
    parsing"才是"这条 UPDATE 没跑过"的可观测证据。
    """
    conv = Conversation(user_id=_SEEDED, title=f"stale-gate-{uuid.uuid4().hex[:8]}")
    db_session.add(conv)
    await db_session.flush()
    att = ChatAttachment(
        user_id=_SEEDED,
        conversation_id=conv.id,
        filename="x.txt",
        original_filename="x.txt",
        mime_type="text/plain",
        size_bytes=1,
        storage_key="/tmp/stale-gate-x.txt",
        status="uploaded",
        parse_status="parsing",
        # 显式压到 10 分钟阈值之外（``_STALE_AFTER``）：server_default 的 now()
        # 永远是"刚刚"，那样的行任何门禁下都不会被扫到。
        created_at=datetime.now(UTC) - timedelta(minutes=20),
    )
    db_session.add(att)
    await db_session.commit()
    return att


async def _parse_status(att_id: uuid.UUID) -> str:
    """另起一个 session 读 —— sweeper 用的是自己的 session，本会话缓存的那份是旧的。"""
    async with TestSessionLocal() as db:
        return (
            await db.execute(
                select(ChatAttachment.parse_status).where(ChatAttachment.id == att_id)
            )
        ).scalar_one()


def _record_dispatch(monkeypatch) -> list[uuid.UUID]:
    """截获重派 seam，返回被派发过的附件 id 列表。

    必须截获：真实派发会走存储与模型（``parse_attachment_now``），本用例要钉的
    只是"这条 UPDATE + requeue 有没有发生"，不是解析本身。
    """
    dispatched: list[uuid.UUID] = []

    def _fake_dispatch(session_factory, attachment_id):
        dispatched.append(attachment_id)

    monkeypatch.setattr(stale_job_recovery, "_schedule_attachment_parse", _fake_dispatch)
    return dispatched


def _count_sweeps(monkeypatch) -> list[int]:
    """数 sweep body 被调用了几次。

    光看"行没变"不够：种子行查不出来时它也一样没变。把 body 的调用次数一起钉住，
    才能区分"非 leader 所以不扫"与"扫了但什么都没查到"。
    """
    calls: list[int] = []
    original = stale_job_recovery.requeue_stale_jobs_once

    async def _spy(session_factory):
        calls.append(1)
        return await original(session_factory)

    monkeypatch.setattr(stale_job_recovery, "requeue_stale_jobs_once", _spy)
    return calls


def _declining_gate(monkeypatch) -> dict[str, int]:
    """把 sweeper 内部构造的 ``LeaderGate`` 换成永远抢不到锁的那个（= standby）。

    换类而不是换实例：``StaleJobSweeper.__init__`` 与 retention 一样是在构造时
    ``from app.core.leader import LeaderGate`` 惰性取名，所以补丁必须在构造前生效。
    """
    calls = {"acquire": 0, "release": 0}

    class _Gate:
        def __init__(self, *args, **kwargs):
            pass

        async def acquire(self) -> bool:
            calls["acquire"] += 1
            return False

        async def release(self) -> None:
            calls["release"] += 1

    monkeypatch.setattr("app.core.leader.LeaderGate", _Gate)
    return calls


async def _run_one_tick(sweeper: StaleJobSweeper) -> None:
    """起循环跑一个 tick 再收尾：``_loop`` 的第一轮在建任务后立即执行，之后才睡间隔。

    这里只是把事件循环让出去，让那一轮真的被调度完（三次内存 SQLite 往返）；
    间隔默认 300s，所以不会跑到第二轮。
    """
    sweeper.start()
    await asyncio.sleep(0.1)
    await sweeper.stop()


async def test_non_leader_replica_writes_nothing(db_session, monkeypatch):
    """抢不到主锁 = 这一 tick 什么都不做：不 SELECT、不 UPDATE、不重派解析。"""
    att = await _stale_parsing_attachment(db_session)
    gate_calls = _declining_gate(monkeypatch)
    sweeps = _count_sweeps(monkeypatch)
    dispatched = _record_dispatch(monkeypatch)

    await _run_one_tick(StaleJobSweeper(TestSessionLocal))

    assert gate_calls["acquire"] >= 1, "循环根本没问过门禁"
    assert not sweeps, "非 leader 副本仍然跑了 sweep"
    assert not dispatched
    assert await _parse_status(att.id) == "parsing", "非 leader 副本改了行"
    # stop() 必须把锁还回去，否则 standby 要等这个进程的连接被回收才能接管。
    assert gate_calls["release"] == 1


async def test_sweeps_still_run_without_advisory_locks(db_session, monkeypatch):
    """无 advisory lock 可用时（SQLite 测试库 / 单进程 dev）回落成"自己就是 leader"。

    门禁开着、走的却是非 Postgres 分支 —— 这正是 retention/recovery 依赖的同一条
    回落路径（``AdvisoryLeader.try_acquire`` 里 dialect 不是 postgresql 就返回
    True）。如果这里改成"抢不到锁就不扫"，本地开发和整套内存 SQLite 测试就会静默
    失去这条恢复路径的覆盖。
    """
    monkeypatch.setattr(get_settings(), "LEADER_ELECTION_ENABLED", True)
    att = await _stale_parsing_attachment(db_session)
    sweeps = _count_sweeps(monkeypatch)
    dispatched = _record_dispatch(monkeypatch)

    await _run_one_tick(StaleJobSweeper(TestSessionLocal))

    assert sweeps, "没有 leader 提供者时 sweeper 不再工作"
    # 只断言"自己那条行"：测试库是跨用例共享的一条连接，上一条用例留下的
    # ``parsing`` 行同样过期，会被这次 sweep 一起捞走。
    assert att.id in dispatched
    assert await _parse_status(att.id) == "pending"


async def test_leader_name_none_forces_unconditional_sweep(db_session, monkeypatch):
    """``leader_name=None`` 是文档化的逃生口：不建门禁，照旧每副本都扫。"""
    att = await _stale_parsing_attachment(db_session)
    gate_calls = _declining_gate(monkeypatch)
    dispatched = _record_dispatch(monkeypatch)

    await _run_one_tick(StaleJobSweeper(TestSessionLocal, leader_name=None))

    assert gate_calls["acquire"] == 0, "显式关掉门禁后还去问锁"
    assert att.id in dispatched
    assert await _parse_status(att.id) == "pending"


async def test_degraded_gate_still_leads_on_later_ticks(monkeypatch):
    """回落不是一次性的：第二个 tick 起也要继续放行。

    ``LeaderGate.acquire`` 在"已经是 leader"之后改问 ``AdvisoryLeader.renew()``
    （"锁还在吗"），而 SQLite 的降级模式压根没有连接 —— 一旦 renew 把"没连接"当成
    "锁丢了"，这个循环（以及同源的 retention 循环）在 dev 与整套内存 SQLite 测试里
    就只会跑第一轮，之后永久静默，正好是本次改动要避免的那种丢覆盖。
    """
    monkeypatch.setattr(get_settings(), "LEADER_ELECTION_ENABLED", True)
    gate = StaleJobSweeper(TestSessionLocal)._gate

    assert await gate.acquire(), "第一个 tick 就被挡：降级没生效"
    assert await gate.acquire(), "第二个 tick 起永久静默"
    assert gate.is_leader


def test_gate_name_does_not_collide_with_the_other_singletons():
    """锁身份由名字哈希而来，所以 ``stale-jobs`` 必须与其它循环的锁名互不相同。

    同名不是"各自一份"而是共用一把锁：先拿到的那个会把另一个循环永久挤出去。最
    不能撞的是 ``alembic-migrate``（deploy/k8s/migrate-job.yaml 用同一个 namespace +
    同一套 ``_lock_key`` 取阻塞锁）—— 撞上就等于一次发版永久挡住恢复循环，或反过来
    让迁移在 ``lock_timeout`` 后失败。
    """
    sweeper = StaleJobSweeper(TestSessionLocal)
    name = sweeper._gate._leader.name

    assert name == "stale-jobs"
    keys = [_lock_key(n) for n in [name, "retention", "recovery", "alembic-migrate"]]
    assert len(set(keys)) == 4, "锁名撞键：stale-jobs 与既有循环/迁移共用一把锁"

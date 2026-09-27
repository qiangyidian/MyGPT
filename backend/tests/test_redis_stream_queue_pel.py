"""Redis 流式队列的 PEL 扫描：一条 backlog 只花两次往返，且截断窗口是配置项。

``XPENDING`` + ``XRANGE`` 这段扫描同时是**去重**（``enqueue`` 靠它判断"这条 run
是不是已经在队列里"）和**收尾**（``ack`` 靠它找到要确认的消息 id）的唯一依据。旧实现在
这里有两个问题，都由本文件钉住：

1. **每条 pending 再问一次 ``XRANGE``** —— 于是 ``enqueue`` 这个最热的路径为了回答
   "已经在队列里吗"，要按 backlog 大小付 N 次网络往返，``ack`` 再付一遍；
2. **窗口大小是写死在四处调用点上的字面量 1000** —— 超过的部分不是"慢一点"，而是
   **看不见**：一条仍然 pending 的 run 会被判定为"没排队"，于是同一个 run 被 XADD
   第二遍，最终执行两次。所以它必须是配置项，并且要能被测试证明"读取的窗口确实来自
   配置"。

顺带钉住两个会让恢复出错的边界：span 之内但不属于 PEL 的消息（已 ack 或更新的条目）
绝不能被 ack —— 那等于把别的消费者正在跑的 run 放回队列；以及正文已被 ``maxlen`` 裁掉
的 pending 条目（读不到 ``run_id``）不出现在结果里。
"""
from __future__ import annotations

import uuid

from app.agents.workflow.queue import RedisStreamQueue
from app.core.config import get_settings


class _FakeStreams:
    """只实现 PEL 扫描用到的那几个命令的假客户端，按插入顺序当作 id 升序。

    记账 ``calls`` 是本文件的主要断言手段：这里要验的是**往返次数**与**参数**，
    光看返回值区分不出"一次 span 扫描"和"N 次单条扫描"。
    """

    def __init__(self, entries: dict[str, str]) -> None:
        self.entries = dict(entries)
        self.calls: list[tuple] = []
        self.acked: list[str] = []

    async def xgroup_create(self, stream, group, id="0", mkstream=False):
        self.calls.append(("xgroup_create", stream, group))

    async def xpending_range(self, stream, group, min="-", max="+", count=None):
        self.calls.append(("xpending_range", count))
        ids = list(self.entries)
        if count is not None:
            ids = ids[:count]
        return [{"message_id": mid} for mid in ids]

    async def xrange(self, stream, min="-", max="+", count=None):
        self.calls.append(("xrange", min, max))
        ids = list(self.entries)
        # span 里可能掺进不属于 PEL 的消息：假客户端如实返回，过滤是实现的职责。
        window = ids[ids.index(min): ids.index(max) + 1] if min in ids and max in ids else []
        return [(mid, {"run_id": self.entries[mid]}) for mid in window]

    async def xack(self, stream, group, *ids):
        self.calls.append(("xack", ids))
        for mid in ids:
            self.acked.append(mid)
            self.entries.pop(mid, None)
        return len(ids)

    async def xadd(self, stream, fields, maxlen=None, approximate=False):
        mid = f"{7_000 + len(self.calls)}-0"
        self.entries[mid] = fields["run_id"]
        self.calls.append(("xadd", mid))
        return mid


def _entries(n: int, run_id: str | None = None) -> dict[str, str]:
    """n 条 pending：id 形如 ``1001-0``…，``run_id`` 全部相同或各不相同。"""
    out: dict[str, str] = {}
    for i in range(n):
        out[f"{1001 + i}-0"] = run_id or str(uuid.uuid4())
    return out


def _count(call_list: list[tuple], name: str) -> int:
    return sum(1 for c in call_list if c[0] == name)


async def test_pending_scan_costs_two_round_trips_whatever_the_backlog():
    """50 条 pending 也只问一次 XPENDING + 一次 XRANGE。

    旧实现是 1 + 50 次：扫描条数与 backlog 成正比，而它在 enqueue/ack 上，也就是每条
    run 的开头和结尾各付一遍。
    """
    run_id = uuid.uuid4()
    entries = _entries(50, str(run_id))
    client = _FakeStreams(entries)
    queue = RedisStreamQueue(client)

    assert await queue._has_pending(run_id) is True
    assert _count(client.calls, "xpending_range") == 1
    assert _count(client.calls, "xrange") == 1, "又退回到每条一次 XRANGE"


async def test_pending_scan_window_comes_from_settings(monkeypatch):
    """窗口大小必须读 ``RUN_STREAM_PENDING_SCAN``，且在参数里真的送出去。

    断言"只看到前 7 条"而不是"看到 7 条是因为数据只有 7 条"：种子放 20 条，配置成 7。
    """
    monkeypatch.setattr(get_settings(), "RUN_STREAM_PENDING_SCAN", 7)
    client = _FakeStreams(_entries(20))
    queue = RedisStreamQueue(client)

    ids = await queue.pending_ids()

    assert len(ids) == 7, "窗口没生效：配置改了扫描范围还是老样子"
    assert ("xpending_range", 7) in client.calls


async def test_window_floor_of_one_keeps_the_scan_alive(monkeypatch):
    """``RUN_STREAM_PENDING_SCAN=0`` 是个打错字的配置，不该让队列瞎掉。

    钳到 1 而不是 0：0 会把每次扫描变成"永远查不到 pending"，于是每条 run 都被重复
    XADD —— 正是这个旋钮要防的那件事。
    """
    monkeypatch.setattr(get_settings(), "RUN_STREAM_PENDING_SCAN", 0)
    queue = RedisStreamQueue(_FakeStreams(_entries(3)))

    assert queue._pending_scan == 1


async def test_ack_confirms_every_duplicate_entry_in_a_single_call():
    """同一 run 的多条 pending（历史遗留/重复入队）一次 XACK 全部确认。

    旧实现逐条 ``xack``：每条一个往返。合并成一次调用还顺带保证了"要么全确认、要么
    整批失败可重试"，比中途抛异常留下半确认状态更接近幂等收尾。
    """
    run_id = uuid.uuid4()
    client = _FakeStreams(_entries(4, str(run_id)))
    queue = RedisStreamQueue(client)

    assert await queue.ack(run_id, "worker-a") is True
    assert _count(client.calls, "xack") == 1
    assert sorted(client.acked) == sorted(_entries(4, str(run_id)))


async def test_ack_returns_false_when_nothing_matches():
    """没有这个 run 的 pending 条目时报 False，而不是"成功"。

    调用方（worker 收尾）据此区分"我确认了"和"这条根本不在我手里"——后者意味着租约
    已经被人接管，日志里必须看得见。
    """
    client = _FakeStreams(_entries(3))
    queue = RedisStreamQueue(client)

    assert await queue.ack(uuid.uuid4(), "worker-a") is False
    assert _count(client.calls, "xack") == 0


async def test_entries_inside_the_span_but_not_pending_are_never_acked():
    """XPENDING 与 XRANGE 的差集不能进结果：span 里的非 pending 消息属于别人。

    这些条目要么已经 ack 过、要么是刚入队还没被取走的新消息。把它们一起确认掉，等于
    悄悄把一个正在执行的 run 从 PEL 里抹掉 —— 那条 run 再也不会被 reclaim，卡到永久。
    """

    class _SpanWiderThanPEL(_FakeStreams):
        async def xpending_range(self, stream, group, min="-", max="+", count=None):
            self.calls.append(("xpending_range", count))
            # 只有 1002-0 在 PEL 里；1003-0 是 span 的端点、1001-0 已被确认过。
            return [{"message_id": "1002-0"}]

        async def xrange(self, stream, min="-", max="+", count=None):
            self.calls.append(("xrange", min, max))
            return [
                ("1001-0", {"run_id": self.entries["1001-0"]}),
                ("1002-0", {"run_id": self.entries["1002-0"]}),
                ("1003-0", {"run_id": self.entries["1003-0"]}),
            ]

    run_id = uuid.uuid4()
    client = _SpanWiderThanPEL(
        {mid: str(run_id) for mid in ("1001-0", "1002-0", "1003-0")}
    )
    queue = RedisStreamQueue(client)

    assert await queue.ack(run_id, "worker-a") is True
    assert client.acked == ["1002-0"], f"多确认了别人的条目: {client.acked}"


async def test_trimmed_bodies_are_skipped_rather_than_guessed():
    """正文已被 ``maxlen`` 裁掉的 pending 条目读不到 run_id —— 跳过，不猜。

    这里没有"退化成按 id 猜一个 run"的空间：猜错会把一条无关的 run 认作已确认。
    这类条目由 :meth:`RedisStreamQueue.reclaim_stale` 显式处理（XCLAIM 回来的空正文
    直接 ack 丢弃），所以扫描侧保持沉默是完整设计的一半。
    """

    class _BodyGone(_FakeStreams):
        async def xrange(self, stream, min="-", max="+", count=None):
            self.calls.append(("xrange", min, max))
            return [("1001-0", {})]  # id 还在 PEL 里，正文没了

    queue = RedisStreamQueue(_BodyGone({"1001-0": str(uuid.uuid4())}))

    assert await queue.pending_ids() == []

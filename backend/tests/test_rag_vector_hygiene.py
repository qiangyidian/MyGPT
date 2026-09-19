"""批次 C ①：向量侧的存储卫生（条目 17）。

两件互补的事：
  * 删知识库时必须真的把整份 Qdrant collection 撤掉。
  * 撤失败（Qdrant 抖动）时不能把用户的删除变成 502，所以另有一条周期性孤儿
    collection 回收兜底 —— 而这条兜底路径**必须**保守到不可能误删在用的库。
"""
from __future__ import annotations

import uuid

from app.services.retention import _kb_hex, sweep_orphan_collections


def _collection(kb_id) -> str:
    return "kb_" + str(kb_id).replace("-", "")


# --------------------------------------------------------------------------- #
# 名字形状
# --------------------------------------------------------------------------- #
def test_only_the_exact_kb_shape_is_a_candidate():
    """共享的 chat_attachments、记忆 collection 等绝不能进回收范围。"""
    kid = uuid.uuid4()
    assert _kb_hex(_collection(kid)) == kid.hex
    for name in (
        "chat_attachments",
        "agent_memories",
        "kb_short",
        "kb_" + "z" * 32,
        "kb_" + kid.hex + "_extra",
        "KB_" + kid.hex,
        "",
        "kb",
    ):
        assert _kb_hex(name) is None, name


# --------------------------------------------------------------------------- #
# 回收
# --------------------------------------------------------------------------- #
class _Store:
    def __init__(self, collections, *, listing_broken=False) -> None:
        self._collections = list(collections)
        self.dropped: list[str] = []
        self._listing_broken = listing_broken

    async def list_collections(self):
        if self._listing_broken:
            raise RuntimeError("qdrant unreachable")
        return list(self._collections)

    async def drop_collection(self, collection):
        self.dropped.append(collection)


def _fake_factory(*, kb_ids=(), broken=False):
    """一个只回答「这些知识库还活着」的伪会话工厂。

    测试库是全 session 共享的 SQLite（StaticPool），真去查 ``knowledge_bases``
    会读到别的用例留下的行 —— 那正好毁掉这里要验证的两个边界（查询失败 / 空
    结果）。所以判定输入直接由用例给。
    """

    class _Ctx:
        async def __aenter__(self):
            class _Db:
                async def execute(self, *_a, **_k):
                    if broken:
                        raise RuntimeError("db down")

                    class _R:
                        def scalars(self):
                            return self

                        def all(self):
                            return list(kb_ids)

                    return _R()

            return _Db()

        async def __aexit__(self, *exc):
            return False

    return lambda: _Ctx()


async def test_sweep_drops_only_collections_with_no_live_kb():
    live, dead = uuid.uuid4(), uuid.uuid4()
    store = _Store([_collection(live), _collection(dead), "chat_attachments"])
    dropped = await sweep_orphan_collections(
        _fake_factory(kb_ids=[live]), vector_store=store
    )
    assert dropped == 1
    # 在用的那个和共享 collection 都不许碰。
    assert store.dropped == [_collection(dead)], store.dropped


async def test_sweep_treats_a_string_uuid_as_live():
    """SQLite 可能把 id 读成 str 而不是 uuid.UUID —— 归一化不能漏。"""
    live = uuid.uuid4()
    store = _Store([_collection(live)])
    assert (
        await sweep_orphan_collections(
            _fake_factory(kb_ids=[str(live)]), vector_store=store
        )
        == 0
    )
    assert store.dropped == []


async def test_a_broken_collection_listing_touches_nothing():
    store = _Store(["kb_" + uuid.uuid4().hex], listing_broken=True)
    assert await sweep_orphan_collections(
        _fake_factory(kb_ids=[uuid.uuid4()]), vector_store=store
    ) == 0
    assert store.dropped == []


async def test_sweep_is_skipped_when_the_kb_query_fails():
    store = _Store(["kb_" + uuid.uuid4().hex])
    assert (
        await sweep_orphan_collections(_fake_factory(broken=True), vector_store=store)
        == 0
    )
    assert store.dropped == []


async def test_sweep_is_skipped_when_no_kb_is_live_at_all():
    """读不到任何知识库 = 分不清「平台是空的」和「查询出错了」，一律不动手。"""
    names = ["kb_" + uuid.uuid4().hex for _ in range(9)]
    store = _Store(names)
    assert (
        await sweep_orphan_collections(_fake_factory(kb_ids=[]), vector_store=store)
        == 0
    )
    assert store.dropped == []


async def test_sweep_respects_the_kill_switch(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "ORPHAN_COLLECTION_SWEEP_ENABLED", False, raising=False
    )
    store = _Store(["kb_" + uuid.uuid4().hex])
    assert (
        await sweep_orphan_collections(
            _fake_factory(kb_ids=[uuid.uuid4()]), vector_store=store
        )
        == 0
    )
    assert store.dropped == []


class _HalfBrokenStore(_Store):
    """第一个 collection 的删除被 Qdrant 拒绝，后面的照常。"""

    async def drop_collection(self, collection):
        self.dropped.append(collection)
        if len(self.dropped) == 1:
            raise RuntimeError("qdrant rejected the drop")


async def test_drop_failure_does_not_abort_the_rest():
    live, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    store = _HalfBrokenStore([_collection(live), _collection(first), _collection(second)])
    dropped = await sweep_orphan_collections(
        _fake_factory(kb_ids=[live]), vector_store=store
    )
    # 失败的那次不计数，但不能挡住后面那个 —— 兜底路径每 6 小时才跑一轮。
    assert dropped == 1
    assert store.dropped == [_collection(first), _collection(second)]


async def test_sweep_stops_at_the_per_run_drop_cap():
    live = uuid.uuid4()
    dead = [uuid.uuid4() for _ in range(5)]
    store = _Store([_collection(live)] + [_collection(k) for k in dead])
    assert (
        await sweep_orphan_collections(
            _fake_factory(kb_ids=[live]), vector_store=store, max_drops=2
        )
        == 2
    )
    assert store.dropped == [_collection(dead[0]), _collection(dead[1])]

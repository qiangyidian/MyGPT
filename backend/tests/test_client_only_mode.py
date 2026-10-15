"""CLIENT_ONLY：客户端实例不起任何会写共享数据的后台角色。

形态来源：本机跑前后端、数据库直连共享（生产）库做测试。那四个角色（摄取 worker /
陈旧任务恢复 / 数据保留清理 / 记忆补做）全都假设「只有我在写这张表」——在共享库上
多起一份，就会去认领别人的文档、删别人的留存数据、重排别人的在途任务。
"""
from __future__ import annotations

import pytest

from app import main as main_module
from app.core.config import get_settings

_BACKGROUND_STATE_ATTRS = (
    "stale_job_sweeper",
    "ingestion_worker",
    "retention_sweeper",
)


@pytest.fixture(autouse=True)
def _no_seed(monkeypatch):
    """建表/灌种子与本测试无关（而且测试库的 engine 与 lifespan 用的不是同一个），
    这里只关心 lifespan 起了哪些后台角色。"""

    async def _noop(app):
        return None

    monkeypatch.setattr(main_module, "init_db", _noop)


async def test_client_only_starts_no_background_writers(monkeypatch):
    monkeypatch.setattr(get_settings(), "CLIENT_ONLY", True)
    app = main_module.create_app()
    async with main_module.lifespan(app):
        for attr in _BACKGROUND_STATE_ATTRS:
            assert not hasattr(app.state, attr), f"{attr} 不该在 CLIENT_ONLY 实例上启动"


async def test_default_instance_still_starts_them(monkeypatch):
    """默认（非 CLIENT_ONLY）行为不变：单机部署照旧自带这些角色。"""
    monkeypatch.setattr(get_settings(), "CLIENT_ONLY", False)
    app = main_module.create_app()
    async with main_module.lifespan(app):
        for attr in _BACKGROUND_STATE_ATTRS:
            assert hasattr(app.state, attr), f"{attr} 在默认真例上必须照旧启动"

"""生产环境不暴露 /docs 与 /openapi.json（条目 38 的「文档面收口」）。

判定的唯一来源是 ``Settings.docs_enabled``：默认按 ENV 派生（dev/test 开、其余关），
``DOCS_ENABLED`` 显式给值时覆盖。第二组用例钉的是「接线真的生效」—— 关掉之后
FastAPI 上这三个路径必须不存在，而不是仍然能被 curl 到。
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app import main as main_module
from app.core.config import Settings


def _prod_settings(**overrides) -> Settings:
    """一台除被测项之外全都配好的生产机器（否则会先撞上别的启动守卫）。"""
    base = {
        "ENV": "prod",
        "JWT_SECRET": Fernet.generate_key().decode(),
        "ADMIN_PASSWORD": "RotatedAdminPass123",
        "FERNET_KEY": Fernet.generate_key().decode(),
        "REDEEM_CODE_PEPPER": Fernet.generate_key().decode(),
    }
    base.update(overrides)
    return Settings(**base)


# ---- 判定口径 --------------------------------------------------------------


def test_docs_are_off_by_default_outside_dev_and_test():
    assert _prod_settings().docs_enabled is False
    assert Settings(ENV="dev").docs_enabled is True
    assert Settings(ENV="test").docs_enabled is True


def test_docs_enabled_can_be_pinned_either_way():
    # 生产临时开一次排查问题要显式说；开发想验证关闭效果也同样能钉住。
    assert _prod_settings(DOCS_ENABLED=True).docs_enabled is True
    assert Settings(ENV="dev", DOCS_ENABLED=False).docs_enabled is False


# ---- 路由真的消失 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("docs_on", "expected"),
    [(False, None), (True, "/docs")],
)
def test_create_app_wires_the_three_doc_routes_together(monkeypatch, docs_on, expected):
    settings = _prod_settings(DOCS_ENABLED=docs_on)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    app = main_module.create_app()
    # 三件套必须同开同关：留着 /openapi.json 等于留着整份路由清单，
    # 只关 /docs 只是把入口藏起来而已。
    assert app.docs_url == expected
    assert app.redoc_url == ("/redoc" if docs_on else None)
    assert app.openapi_url == ("/openapi.json" if docs_on else None)


# ---- 测试环境（ENV=test）仍然打得开 ---------------------------------------


async def test_docs_stay_reachable_in_test_env(client):
    assert (await client.get("/docs")).status_code == 200
    assert (await client.get("/openapi.json")).status_code == 200

"""Fernet / pepper 启动门禁与轮换的纯函数测试（不碰数据库、不碰网络）。

覆盖三件事：
  1. 占位/演示/畸形 key 的判定口径（``is_placeholder_secret`` /
     ``fernet_key_problem`` / ``redeem_code_pepper_problem``）；
  2. ``Settings`` 构造期真的会拒绝它们，并且给出的是一句能照着做的中文错误；
  3. ``FERNET_KEYS=新,旧`` 的轮换语义：新的加密、历史任一把仍能解密。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.core import security
from app.core.config import (
    _DEMO_FERNET_KEYS,
    Settings,
    fernet_key_problem,
    is_placeholder_secret,
    redeem_code_pepper_problem,
)

KEY_NEW = Fernet.generate_key().decode()
KEY_OLD = Fernet.generate_key().decode()
KEY_THIRD = Fernet.generate_key().decode()
DEMO_KEY = _DEMO_FERNET_KEYS[0]
# 44 字符但不是合法 Fernet token：长度这一关过得去，构造那一关过不去。
MALFORMED_KEY = "!" * 44


def _prod(**overrides) -> Settings:
    """一台「除被测项之外全都配好」的生产机器。"""
    base = {
        "ENV": "prod",
        "JWT_SECRET": Fernet.generate_key().decode(),
        "ADMIN_PASSWORD": "RotatedAdminPass123",
        "FERNET_KEY": KEY_NEW,
        "REDEEM_CODE_PEPPER": Fernet.generate_key().decode(),
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def clean_fernet_cache():
    """``_fernet()`` 有两把模块级缓存，改完必须还原，否则污染同批测试。"""
    saved = (security._FERNET_CACHE, security._FALLBACK_FERNET, security.settings)
    security._FERNET_CACHE = None
    security._FALLBACK_FERNET = None
    try:
        yield
    finally:
        security._FERNET_CACHE, security._FALLBACK_FERNET, security.settings = saved


# ---- 判定口径 --------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        DEMO_KEY,
        DEMO_KEY.upper(),
        "CHANGE_ME",
        "change-me-please",
        "changeme",
        "replace_me_now",
        "your-secret-key",
        "<paste-key-here>",
    ],
)
def test_placeholder_values_are_placeholders(value: str):
    assert is_placeholder_secret(value) is True


@pytest.mark.parametrize("value", [KEY_NEW, KEY_OLD, "a-real-looking-secret-value"])
def test_real_values_are_not_placeholders(value: str):
    assert is_placeholder_secret(value) is False


def test_valid_fernet_key_has_no_problem():
    assert fernet_key_problem(KEY_NEW) is None


def test_placeholder_fernet_key_reports_in_chinese():
    problem = fernet_key_problem(DEMO_KEY)
    assert problem is not None and "占位" in problem


def test_wrong_length_fernet_key_reports_length():
    problem = fernet_key_problem(KEY_NEW[:20])
    assert problem is not None and "44" in problem


def test_malformed_fernet_key_is_rejected_before_request_path():
    problem = fernet_key_problem(MALFORMED_KEY)
    assert problem is not None and "合法" in problem


@pytest.mark.parametrize(
    ("value", "fragment"),
    [("", "未配置"), ("your-pepper", "演示"), ("short", "太短")],
)
def test_pepper_problems(value: str, fragment: str):
    problem = redeem_code_pepper_problem(value)
    assert problem is not None and fragment in problem


def test_strong_pepper_is_accepted():
    assert redeem_code_pepper_problem(Fernet.generate_key().decode()) is None


# ---- 构造期拒绝（生产） ----------------------------------------------------


def test_prod_rejects_missing_fernet_key():
    with pytest.raises(ValueError, match="未配置 FERNET_KEY"):
        _prod(FERNET_KEY="", FERNET_KEYS="")


def test_prod_rejects_demo_fernet_key():
    with pytest.raises(ValueError, match="占位"):
        _prod(FERNET_KEY=DEMO_KEY)


def test_prod_rejects_change_me_fernet_key():
    with pytest.raises(ValueError, match="占位"):
        _prod(FERNET_KEY="CHANGE_ME_BEFORE_DEPLOY")


def test_prod_rejects_malformed_fernet_key():
    with pytest.raises(ValueError, match="合法"):
        _prod(FERNET_KEY=MALFORMED_KEY)


def test_prod_error_tells_the_operator_what_to_run():
    with pytest.raises(ValueError) as excinfo:
        _prod(FERNET_KEY=MALFORMED_KEY)
    message = str(excinfo.value)
    assert "Fernet.generate_key" in message
    assert "FERNET_KEYS=" in message


def test_prod_rejects_demo_pepper():
    with pytest.raises(ValueError, match="REDEEM_CODE_PEPPER"):
        _prod(REDEEM_CODE_PEPPER="your-pepper")


def test_prod_rejects_empty_pepper():
    with pytest.raises(ValueError, match="REDEEM_CODE_PEPPER"):
        _prod(REDEEM_CODE_PEPPER="")


def test_prod_accepts_rotation_without_single_key():
    """FERNET_KEYS 单独配就是文档里的轮换形态，不能被当成「没配」。"""
    settings = _prod(FERNET_KEY="", FERNET_KEYS=f"{KEY_NEW},{KEY_OLD}")
    assert settings.fernet_keys == [KEY_NEW, KEY_OLD]


@pytest.mark.parametrize("env", ["dev", "test"])
def test_dev_and_test_only_warn_about_the_repo_demo_key(env: str, caplog):
    """仓库自带的 .env 就是演示值：本地和测试要起得来，但必须吭一声。"""
    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        settings = Settings(
            ENV=env,
            FERNET_KEY=DEMO_KEY,
            REDEEM_CODE_PEPPER="",
            JWT_SECRET="please-change-this",
            ADMIN_PASSWORD="changeme123",
        )
    assert settings.fernet_keys == [DEMO_KEY]
    assert any("Fernet.generate_key" in rec.getMessage() for rec in caplog.records)


def test_dev_still_rejects_a_malformed_key():
    """手抄错不是「开发环境的自由」，任何环境都得起不来。"""
    with pytest.raises(ValueError, match="合法"):
        Settings(ENV="dev", FERNET_KEY=MALFORMED_KEY)


# ---- fernet_keys 解析 ------------------------------------------------------


@pytest.mark.parametrize(
    ("keys_env", "key_env", "expected"),
    [
        (f"{KEY_NEW},{KEY_OLD}", KEY_OLD, [KEY_NEW, KEY_OLD]),
        (f"  {KEY_NEW} , {KEY_OLD} ", KEY_THIRD, [KEY_NEW, KEY_OLD, KEY_THIRD]),
        ("", KEY_NEW, [KEY_NEW]),
        (f"{KEY_NEW},{KEY_NEW}", KEY_NEW, [KEY_NEW]),
    ],
)
def test_fernet_keys_ordering_and_dedupe(keys_env: str, key_env: str, expected):
    settings = Settings(ENV="test", FERNET_KEYS=keys_env, FERNET_KEY=key_env)
    assert settings.fernet_keys == expected


def test_quoted_key_from_an_env_file_is_unwrapped():
    settings = Settings(ENV="test", FERNET_KEY=f'"{KEY_NEW}"')
    assert settings.fernet_keys == [KEY_NEW]
    assert fernet_key_problem(settings.fernet_keys[0]) is None


# ---- 请求路径的兜底 --------------------------------------------------------


def test_fernet_propagates_a_malformed_key_as_an_actionable_error(clean_fernet_cache):
    security.settings = SimpleNamespace(fernet_keys=[KEY_NEW, MALFORMED_KEY])
    with pytest.raises(RuntimeError) as excinfo:
        security._fernet()
    message = str(excinfo.value)
    assert "第 2 项" in message
    assert "Fernet.generate_key" in message


def test_fernet_only_warns_about_a_placeholder(clean_fernet_cache, caplog):
    """dev/test 的演示 key 要能用；请求路径不能因此炸。"""
    security.settings = SimpleNamespace(fernet_keys=[DEMO_KEY])
    with caplog.at_level(logging.WARNING, logger="app.core.security"):
        rotator = security._fernet()
    assert rotator.encrypt(b"ok")
    assert any("占位" in rec.getMessage() for rec in caplog.records)


def test_rotator_is_cached_per_key_list(clean_fernet_cache):
    security.settings = SimpleNamespace(fernet_keys=[KEY_NEW, KEY_OLD])
    first = security._fernet()
    assert security._fernet() is first
    security.settings = SimpleNamespace(fernet_keys=[KEY_OLD])
    assert security._fernet() is not first


# ---- 轮换语义 --------------------------------------------------------------


def test_new_key_encrypts_and_old_key_still_decrypts(clean_fernet_cache):
    security.settings = SimpleNamespace(fernet_keys=[KEY_OLD])
    legacy = security.encrypt_secret("sk-legacy")

    security.settings = SimpleNamespace(fernet_keys=[KEY_NEW, KEY_OLD])
    fresh = security.encrypt_secret("sk-fresh")

    # 老密文没被重写，也还能解 —— 这正是 FERNET_KEYS 存在的理由。
    assert security.decrypt_secret(legacy) == "sk-legacy"
    assert security.decrypt_secret(fresh) == "sk-fresh"
    # 新密文必须由新 key 解出来（=列表第 0 把在加密）。
    assert Fernet(KEY_NEW.encode()).decrypt(fresh.encode()).decode() == "sk-fresh"
    with pytest.raises(InvalidToken):
        Fernet(KEY_OLD.encode()).decrypt(fresh.encode())


def test_dropping_the_retired_key_locks_out_its_ciphertext(clean_fernet_cache):
    security.settings = SimpleNamespace(fernet_keys=[KEY_OLD])
    legacy = security.encrypt_secret("sk-legacy")

    security.settings = SimpleNamespace(fernet_keys=[KEY_NEW])
    assert security.decrypt_secret(legacy) == ""


def test_decrypt_never_raises_on_garbage(clean_fernet_cache):
    security.settings = SimpleNamespace(fernet_keys=[KEY_NEW])
    assert security.decrypt_secret("not-a-fernet-token") == ""
    assert security.encrypt_secret("") == ""
    assert security.decrypt_secret("") == ""

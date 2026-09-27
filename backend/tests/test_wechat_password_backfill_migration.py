"""迁移 0020：微信 ``password_hash`` 哨兵回填的边界。

为什么值得单测：这一步 **UPDATE 真实用户行**，判错的代价是把一个自己设过密码的
账号的密码清掉（他从此无法用密码登录）。所以测试不验「跑通了」，验的是四类必须
被区别对待的行：该改的、不该改的、已经是哨兵的、以及最容易踩的「老账号主动绑定
微信」。

沿用 ``test_model_capabilities_migration.py`` 的做法：子进程跑 alembic + 临时
SQLite 文件，这样测的是真实的迁移链路（升到上一头 → 造数据 → 升到 head），而不是
把迁移函数单独抠出来调用。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

from app.core.security import PASSWORD_NOT_SET

PREVIOUS_HEAD = "0019_ingest_claim_version"
BACKEND_DIR = Path(__file__).resolve().parents[1]

# 一串「看着像 argon2 但没人知道原文」的哈希：正是哨兵方案之前自动注册写入的东西。
RANDOM_LOOKING = "$argon2id$v=19$m=65536,t=3,p=4$Zm9v$YmFy"


def _alembic(database_path: Path, *args: str) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{database_path.as_posix()}",
        "ENV": "test",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _insert_user(
    conn: sqlite3.Connection,
    *,
    email: str,
    password_hash: str,
    username: str | None = None,
) -> str:
    user_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users (id, email, username, password_hash, role, is_active,"
        " token_version, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'user', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (user_id, email, username or email.split("@")[0], password_hash),
    )
    return user_id


def _bind_wechat(conn: sqlite3.Connection, user_id: str, openid: str) -> None:
    conn.execute(
        "INSERT INTO wechat_identities (id, openid, user_id, bound_at)"
        " VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
        (str(uuid.uuid4()), openid, user_id),
    )


def _audit(conn: sqlite3.Connection, user_id: str, action: str) -> None:
    conn.execute(
        "INSERT INTO audit_events (id, actor_id, action, target, created_at)"
        " VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
        (str(uuid.uuid4()), user_id, action, f"user:{user_id}"),
    )


def _hashes(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        email: password_hash
        for email, password_hash in conn.execute(
            "SELECT email, password_hash FROM users"
        ).fetchall()
    }


def test_backfill_hits_only_pre_sentinel_wechat_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "wechat-backfill.sqlite3"
    _alembic(database_path, "upgrade", PREVIOUS_HEAD)

    with sqlite3.connect(database_path) as conn:
        # 1) 该改：哨兵之前自动注册的微信行（合成邮箱 + 随机哈希 + 没有任何设密事件）
        auto = _insert_user(conn, email="wx_oAUTO@wechat.local", password_hash=RANDOM_LOOKING)
        _bind_wechat(conn, auto, "oAUTO")
        # 2) 不该改：自动注册之后本人真的设过密码
        set_pw = _insert_user(conn, email="wx_oSET@wechat.local", password_hash=RANDOM_LOOKING)
        _bind_wechat(conn, set_pw, "oSET")
        _audit(conn, set_pw, "auth:password_changed")
        # 3) 不该改：最容易踩的一类 —— 自己注册的账号后来绑了微信，密码是自己的
        normal = _insert_user(conn, email="li@example.com", password_hash=RANDOM_LOOKING)
        _bind_wechat(conn, normal, "oBIND")
        # 4) 不该改：密码是管理端重置的，同样有真实凭据
        reset = _insert_user(conn, email="wx_oRESET@wechat.local", password_hash=RANDOM_LOOKING)
        _bind_wechat(conn, reset, "oRESET")
        _audit(conn, reset, "auth:password_reset")
        # 5) 已是哨兵：幂等，且不能被改成别的东西
        sentinel = _insert_user(conn, email="wx_oNEW@wechat.local", password_hash=PASSWORD_NOT_SET)
        _bind_wechat(conn, sentinel, "oNEW")
        conn.commit()

    _alembic(database_path, "upgrade", "head")

    with sqlite3.connect(database_path) as conn:
        got = _hashes(conn)
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]

    from app.core.health import REPO_MIGRATION_HEAD  # 动态解析，绝不写死

    assert revision == REPO_MIGRATION_HEAD
    assert got["wx_oAUTO@wechat.local"] == PASSWORD_NOT_SET
    assert got["wx_oSET@wechat.local"] == RANDOM_LOOKING
    assert got["li@example.com"] == RANDOM_LOOKING
    assert got["wx_oRESET@wechat.local"] == RANDOM_LOOKING
    assert got["wx_oNEW@wechat.local"] == PASSWORD_NOT_SET


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    """重跑一次（例如生产上手工 downgrade 再 upgrade）不能改动已经被回填的行。"""
    database_path = tmp_path / "wechat-backfill-idem.sqlite3"
    _alembic(database_path, "upgrade", PREVIOUS_HEAD)
    with sqlite3.connect(database_path) as conn:
        user_id = _insert_user(
            conn, email="wx_oAGAIN@wechat.local", password_hash=RANDOM_LOOKING
        )
        _bind_wechat(conn, user_id, "oAGAIN")
        conn.commit()

    _alembic(database_path, "upgrade", "head")
    _alembic(database_path, "downgrade", PREVIOUS_HEAD)
    _alembic(database_path, "upgrade", "head")

    with sqlite3.connect(database_path) as conn:
        assert _hashes(conn)["wx_oAGAIN@wechat.local"] == PASSWORD_NOT_SET

"""``STORAGE_BACKEND`` 只认 ``local``：选了未实现的后端必须在启动期响。

MinIO/S3 那条分支从来没有实现过（:func:`app.core.storage.get_storage` 直接
``raise``），但配置面、README 与 ``deploy/k8s/config.yaml`` 都曾把它写成生产默认。
那种组合的失败时机是**第一个用户上传文件的时候**：进程活着、健康检查绿、备份策略与
桶名都按对象存储的前提配好了，然后 500。把非法值挪到构造期，部署时就没法看不见。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.storage import get_storage


@pytest.mark.parametrize("raw", ["minio", "s3", "MinIO", "oss", "postgres", "1"])
def test_unimplemented_storage_backend_raises_at_construction(raw):
    """任何非 local 的取值都在 ``Settings()`` 构造期失败，而不是留到第一次上传。"""
    with pytest.raises(ValidationError, match="STORAGE_BACKEND"):
        Settings(ENV="test", STORAGE_BACKEND=raw)


@pytest.mark.parametrize("raw", ["local", "LOCAL", " local ", "", "   "])
def test_local_is_normalized_rather_than_rejected(raw):
    """大小写与空白是手误，空值表示"没配" —— 都归一化成 local 后放行。

    归一化必须发生在**校验器**里：``get_storage`` 自己也 lower 一遍，但那里已经没有
    第二条路可走，让 ``Settings.STORAGE_BACKEND`` 就是规范值，读它的地方才不必各自
    再猜一次大小写。把空值判成非法会更"严格"，代价是现存那份写着 ``STORAGE_BACKEND=``
    的 .env 直接起不来 —— 而它想要的从来就是默认的本机存储。
    """
    assert Settings(ENV="test", STORAGE_BACKEND=raw).STORAGE_BACKEND == "local"


def test_default_settings_keep_local():
    """默认值仍是 local：这道闸门不能反过来把现有部署挡在门外。"""
    assert Settings(ENV="test").STORAGE_BACKEND == "local"


def test_get_storage_rejects_explicit_backend_override():
    """显式传参的那条路也要拒绝。

    启动校验管不到 ``get_storage(backend=...)``（那是测试与未来调用方的入口），所以
    运行时那条 ``raise`` 是这道防线的第二层，不是历史遗留。
    """
    with pytest.raises(NotImplementedError, match="未实现"):
        get_storage("minio")


def test_get_storage_returns_local_backend_for_default_settings():
    from app.core.storage import LocalStorage

    assert isinstance(get_storage("local"), LocalStorage)

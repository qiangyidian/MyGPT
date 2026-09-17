"""Shared test doubles for the WeChat Official Account suites.

Kept out of the test modules so both ``test_wechat_mp.py`` (service/protocol)
and ``test_wechat_mp_api.py`` (HTTP) exercise the same fake.
"""
from __future__ import annotations


class FakeRedis:
    """Minimal async stand-in for the redis surface wechat_mp_service uses.

    ``supports_getdel`` lets a test force the older-server fallback path
    (Redis < 6.2 has no GETDEL), which must still consume the code.
    """

    def __init__(self, *, supports_getdel: bool = True) -> None:
        self.kv: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.supports_getdel = supports_getdel

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def getdel(self, key):
        if not self.supports_getdel:
            raise AttributeError("GETDEL requires Redis >= 6.2")
        return self.kv.pop(key, None)

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if self.kv.pop(k, None) is not None:
                n += 1
        return n

    async def incr(self, key):
        self.kv[key] = str(int(self.kv.get(key, "0")) + 1)
        return int(self.kv[key])

    async def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    async def ttl(self, key):
        return self.ttls.get(key, -2)

"""把用户输入变成 SQL LIKE / ILIKE 模式。

``%`` 和 ``_`` 是用户真能打出来的字符：不转义时搜「100%」会命中一切、搜「a_b」
会静默变成「a 任意字符 b」。搜索结果错了，而用户没有任何线索发现自己写错了什么，
所以这类 bug 只能靠"永远不要在别处再写一遍"来防。

全仓所有从用户输入拼 LIKE 模式的地方都必须走这里（历史上它抄了四份）。
"""
from __future__ import annotations

LIKE_ESCAPE = "\\"


def escape_like(term: str) -> str:
    """转义反斜杠与两个通配符，让它们在模式里只代表自己。"""
    return (
        term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )


def like_pattern(term: str) -> str:
    """两侧夹 ``%`` 的子串匹配模式（调用方自己带 ``escape=LIKE_ESCAPE``）。"""
    return f"%{escape_like(term)}%"

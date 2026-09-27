"""进入模型上下文的外部内容围栏（见 :mod:`app.security.prompt_boundary`）。"""

from app.security.prompt_boundary import (
    OPEN_PREFIX,
    apply_untrusted_boundary,
    close_marker,
    fence_open_marker,
    is_fenced,
    neutralize_fence_tokens,
)

__all__ = [
    "OPEN_PREFIX",
    "apply_untrusted_boundary",
    "close_marker",
    "fence_open_marker",
    "is_fenced",
    "neutralize_fence_tokens",
]

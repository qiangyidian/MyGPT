"""积分核心逻辑测试：扣分公式与兑换码编解码。纯函数，不碰数据库。"""
from __future__ import annotations


from app.credits import (
    CROCKFORD_ALPHABET,
    CreditPolicy,
    code_prefix,
    compute_charge,
    generate_code,
    hash_code,
    normalize_code,
)

POLICY = CreditPolicy(
    enforced=False,
    credits_per_usd=1000.0,
    credits_per_1k_tokens=1.0,
    signup_bonus=0,
    max_adjust=10_000_000,
    max_codes_per_batch=5000,
)


# ---- 扣分公式 --------------------------------------------------------- #

def test_charge_uses_cost_when_known():
    # 0.0123 USD * 1000 = 12.3 -> 向上取整 13
    assert compute_charge(0.0123, 5000, POLICY) == 13


def test_charge_rounds_up_so_a_real_turn_never_costs_zero():
    # 0.0001 USD * 1000 = 0.1 -> 向上取整 1，不能是 0
    assert compute_charge(0.0001, 10, POLICY) == 1


def test_charge_falls_back_to_tokens_when_cost_is_none():
    """未配置定价的模型 usage_cost() 返回 None；没有兜底就是免费额度。"""
    # 3500 tokens / 1000 * 1.0 = 3.5 -> 4
    assert compute_charge(None, 3500, POLICY) == 4


def test_charge_falls_back_to_tokens_when_cost_is_zero():
    assert compute_charge(0.0, 2000, POLICY) == 2


def test_charge_is_zero_when_nothing_was_consumed():
    assert compute_charge(None, None, POLICY) == 0
    assert compute_charge(0.0, 0, POLICY) == 0
    assert compute_charge(None, 0, POLICY) == 0


def test_charge_prefers_cost_over_tokens():
    # cost 已知时完全忽略 token 数量
    assert compute_charge(0.002, 10_000_000, POLICY) == 2


# ---- 码的生成与规范化 -------------------------------------------------- #

def test_generated_code_has_four_groups_of_four():
    code = generate_code()
    parts = code.split("-")
    assert len(parts) == 4
    assert all(len(p) == 4 for p in parts)


def test_generated_code_only_uses_crockford_alphabet():
    for _ in range(200):
        normalized = normalize_code(generate_code())
        assert len(normalized) == 16
        assert set(normalized) <= set(CROCKFORD_ALPHABET)


def test_generated_codes_are_unique():
    assert len({generate_code() for _ in range(500)}) == 500


def test_normalize_accepts_lowercase_and_missing_dashes():
    code = generate_code()
    assert normalize_code(code.lower().replace("-", "")) == normalize_code(code)


def test_normalize_maps_ambiguous_characters():
    """手抄错误：O 打成 0、I/L 打成 1，应该仍然能兑换。"""
    assert normalize_code("oooo-1111-llll-O0I1") == "0000111111110011"


def test_normalize_strips_arbitrary_separators():
    assert normalize_code(" ab12 cd34 ef56 gh78 ") == "AB12CD34EF56GH78"
    assert normalize_code("AB12/ CD34_EF56.GH78") == "AB12CD34EF56GH78"


def test_normalize_is_idempotent():
    code = generate_code()
    once = normalize_code(code)
    assert normalize_code(once) == once


def test_normalize_folds_fullwidth_form_to_ascii():
    """全角输入（ＡＢ１２…）与 ASCII 原码归一化到同一结果。

    中文用户粘贴全角码是现实场景：NFKC 把全角拉丁字母/数字折叠回 ASCII，
    之后走原有的过滤与手抄修正，不会再误报"兑换码不存在"。
    """
    code = generate_code()
    fullwidth = code.translate(
        {ord(c): ord(c) + 0xFEE0 for c in code if ord(c) >= 0x21 and ord(c) <= 0x7E}
    )
    assert fullwidth != code  # 确认翻译确实发生了
    assert normalize_code(fullwidth) == normalize_code(code)
    assert normalize_code(fullwidth) == normalize_code(code.upper())


def test_normalize_folds_fullwidth_digit_to_ascii():
    assert normalize_code("１２３４") == "1234"


def test_normalize_folds_fullwidth_handcopy_fixes_apply_after_nkfc():
    """NFKC 折叠之后，手抄修正（O→0、I/L→1）仍然按顺序生效。"""
    assert normalize_code("ＯＯ１１") == "0011"
    assert normalize_code("ＩＬＬ１") == "1111"


def test_hash_is_64_hex_and_depends_on_normalized_form():
    normalized = normalize_code(generate_code())
    digest = hash_code(normalized)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_ignores_input_formatting():
    code = generate_code()
    assert hash_code(normalize_code(code)) == hash_code(normalize_code(code.lower()))


def test_code_prefix_is_six_chars():
    assert code_prefix("AB12CD34EF56GH78") == "AB12CD"


# ---- 策略解析 ---------------------------------------------------------- #

def test_enforcement_is_forced_off_in_test_env():
    """与 quotas.py / rate_limit.py 的既有约定一致：测试套件默认不被拦截。"""
    from app.core.config import get_settings

    policy = CreditPolicy.from_settings(get_settings())
    assert policy.enforced is False


def test_policy_overrides_are_injectable():
    from dataclasses import replace

    from app.credits import get_credit_policy, set_credit_policy

    set_credit_policy(replace(POLICY, enforced=True))
    try:
        assert get_credit_policy().enforced is True
    finally:
        set_credit_policy(None)


# ---- 兑换码哈希的 pepper ------------------------------------------------ #
def test_hash_is_deterministic_for_the_same_settings(monkeypatch):
    from types import SimpleNamespace

    import app.credits as credits_mod

    monkeypatch.setattr(
        credits_mod,
        "get_settings",
        lambda: SimpleNamespace(REDEEM_CODE_PEPPER="pepper-a", JWT_SECRET="sk"),
    )
    assert credits_mod.hash_code("AB12CD34EF56GH78") == credits_mod.hash_code(
        "AB12CD34EF56GH78"
    )
    assert len(credits_mod.hash_code("AB12CD34EF56GH78")) == 64


def test_hash_differs_across_peppers(monkeypatch):
    """同一张码在两个 pepper 下必须得到不同摘要 —— 否则 pepper 是摆设。"""
    from types import SimpleNamespace

    import app.credits as credits_mod

    monkeypatch.setattr(
        credits_mod,
        "get_settings",
        lambda: SimpleNamespace(REDEEM_CODE_PEPPER="pepper-a", JWT_SECRET="sk"),
    )
    a = credits_mod.hash_code("AB12CD34EF56GH78")
    monkeypatch.setattr(
        credits_mod,
        "get_settings",
        lambda: SimpleNamespace(REDEEM_CODE_PEPPER="pepper-b", JWT_SECRET="sk"),
    )
    b = credits_mod.hash_code("AB12CD34EF56GH78")
    assert a != b


def test_hash_is_not_a_bare_sha256_of_the_code(monkeypatch):
    """防回归：不能退化回裸 SHA-256 —— 那正是 6 字符前缀可被暴力的形态。"""
    import hashlib
    from types import SimpleNamespace

    import app.credits as credits_mod

    monkeypatch.setattr(
        credits_mod,
        "get_settings",
        lambda: SimpleNamespace(REDEEM_CODE_PEPPER="pepper-a", JWT_SECRET="sk"),
    )
    bare = hashlib.sha256(b"AB12CD34EF56GH78").hexdigest()
    assert credits_mod.hash_code("AB12CD34EF56GH78") != bare


def test_hash_falls_back_to_secret_key_derivation_when_pepper_empty(monkeypatch):
    """pepper 缺省时必须仍与 SECRET_KEY 相关，不许静默退化为无密钥哈希。"""
    from types import SimpleNamespace

    import app.credits as credits_mod

    monkeypatch.setattr(
        credits_mod,
        "get_settings",
        lambda: SimpleNamespace(REDEEM_CODE_PEPPER="", JWT_SECRET="sk-1"),
    )
    h1 = credits_mod.hash_code("AB12CD34EF56GH78")
    monkeypatch.setattr(
        credits_mod,
        "get_settings",
        lambda: SimpleNamespace(REDEEM_CODE_PEPPER="", JWT_SECRET="sk-2"),
    )
    h2 = credits_mod.hash_code("AB12CD34EF56GH78")
    assert h1 != h2

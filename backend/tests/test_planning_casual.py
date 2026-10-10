"""Casual detection must not suppress retrieval or research for real queries."""
from __future__ import annotations

import pytest

from app.agents.planning import is_casual_question, should_recognize_intent


@pytest.mark.parametrize(
    "content",
    [
        "你好", "哈喽", "你好你都能干什么", "您好，你能做什么呢？",
        "你好，请介绍一下自己", "早上好，请介绍一下你自己",
        "Hi, who are you?", "Hello, what can you do?", "thank you!",
        "谢谢啦！", "嗯", "ok", "好的呀", "请介绍一下自己",
    ],
)
def test_casual_turn_skips_intent_recognition(content):
    assert is_casual_question(content) is True
    assert should_recognize_intent(content) is False


@pytest.mark.parametrize(
    "content",
    [
        "", "  ", "天气", "汇率", "股票", "你好天气", "您好，天气",
        "你好，查天气", "你好，帮我查天气", "你好，请帮我分析季度销售数据",
        "你能做什么报表", "介绍一下自己的数据库结构", "help me research LLMs",
        "history", "早稻", "你好，请深入调研大模型微调",
    ],
)
def test_real_query_is_not_casual(content):
    assert is_casual_question(content) is False


def test_greeting_with_task_still_recognizes_intent():
    assert should_recognize_intent("你好，帮我查天气") is True

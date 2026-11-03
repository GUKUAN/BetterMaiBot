from types import SimpleNamespace

import pytest

from src.maisaka.planner_task import PlannerTaskState
from src.maisaka.sub_planner.prompt import build_prompt, parse_decision


def _message(text: str, nickname: str = "老王", message_id: str = "m1") -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        processed_plain_text=text,
        message_info=SimpleNamespace(user_info=SimpleNamespace(user_nickname=nickname)),
    )


def _task() -> PlannerTaskState:
    task = PlannerTaskState.open(_message("今晚八点开服", message_id="anchor"))
    task.remember_reasoning("先确认端口，再决定要不要重启")
    return task


def test_prompt_carries_task_and_messages() -> None:
    prompt = build_prompt([_message("先别重启，端口改成 8080")], _task())

    assert "【当前任务】" in prompt
    assert "今晚八点开服" in prompt
    assert "先确认端口" in prompt
    assert "【新到的消息】" in prompt
    assert "端口改成 8080" in prompt
    assert "只输出 JSON" in prompt


def test_prompt_truncates_long_text() -> None:
    prompt = build_prompt([_message("长" * 500)], _task())
    assert "…" in prompt
    assert "长" * 500 not in prompt


def test_prompt_without_anchor() -> None:
    task = PlannerTaskState.open(None)
    prompt = build_prompt([_message("在吗")], task)
    assert "触发消息：未知" in prompt


@pytest.mark.parametrize("kind", ["continue", "update", "interrupt", "defer"])
def test_parse_accepts_all_decisions(kind: str) -> None:
    decision = parse_decision('{"decision":"%s","reason":"测试"}' % kind)

    assert decision is not None
    assert decision.kind == kind
    assert decision.by_rule is False


def test_parse_strips_code_fence() -> None:
    decision = parse_decision('```json\n{"decision":"defer","reason":"另一个问题"}\n```')

    assert decision is not None
    assert decision.kind == "defer"


def test_parse_missing_reason_falls_back() -> None:
    decision = parse_decision('{"decision":"continue"}')

    assert decision is not None
    assert decision.reason


@pytest.mark.parametrize("raw", ["", "我觉得应该打断", '{"decision":"瞎写"}', "{不是 json}"])
def test_parse_rejects_invalid_output(raw: str) -> None:
    # 解析不出来时调用方按「不影响主思考」处理，绝不能默认打断
    assert parse_decision(raw) is None

from types import SimpleNamespace
import asyncio

import pytest

from src.maisaka import runtime as runtime_module
from src.maisaka.runtime import MaisakaHeartFlowChatting, PlannerInterruptController


def _runtime(monkeypatch, *, steering: bool) -> MaisakaHeartFlowChatting:
    monkeypatch.setattr(
        runtime_module,
        "global_config",
        SimpleNamespace(chat=SimpleNamespace(reply_timing=SimpleNamespace(planner_message_steering=steering))),
    )
    instance = object.__new__(MaisakaHeartFlowChatting)
    instance._agent_state = MaisakaHeartFlowChatting._STATE_RUNNING
    instance._planner_interrupt = PlannerInterruptController()
    instance.log_prefix = "[test]"
    instance.message_cache = []
    instance._last_processed_index = 0
    return instance


@pytest.mark.parametrize("steering", [False, True])
def test_only_quick_reaction_mode_interrupts_planner(monkeypatch, steering: bool) -> None:
    runtime = _runtime(monkeypatch, steering=steering)
    interrupt_flag = asyncio.Event()
    runtime._planner_interrupt.bind(interrupt_flag)

    runtime._request_planner_interrupt_for_message(SimpleNamespace(message_id="m1"))

    assert interrupt_flag.is_set() is steering


def test_same_planner_request_is_interrupted_once() -> None:
    controller = PlannerInterruptController()
    assert controller.request() == "idle"

    interrupt_flag = asyncio.Event()
    controller.bind(interrupt_flag)
    assert controller.request() == "request"
    assert controller.request() == "duplicate"

    controller.unbind(interrupt_flag)
    assert controller.request() == "idle"


def test_interrupt_budget_accumulates_across_requests() -> None:
    """预算按整轮统计：换一次 LLM 请求不会重新发额度。"""
    controller = PlannerInterruptController(budget=2)

    controller.bind(asyncio.Event())
    assert controller.request() == "request"
    assert controller.has_budget_left() is True

    controller.bind(asyncio.Event())
    assert controller.request() == "request"
    assert controller.has_budget_left() is False

    controller.bind(asyncio.Event())
    assert controller.request() == "exhausted"


def test_zero_budget_keeps_unlimited_behaviour() -> None:
    """0 表示不限，等价于引入预算之前的行为。"""
    controller = PlannerInterruptController()

    for _ in range(5):
        controller.bind(asyncio.Event())
        assert controller.request() == "request"


def test_reset_budget_restores_allowance() -> None:
    controller = PlannerInterruptController(budget=1)
    controller.bind(asyncio.Event())
    assert controller.request() == "request"
    assert controller.has_budget_left() is False

    controller.reset_budget(1)
    controller.bind(asyncio.Event())
    assert controller.request() == "request"


def test_exhausted_budget_does_not_set_interrupt_flag(monkeypatch) -> None:
    runtime = _runtime(monkeypatch, steering=True)
    runtime._planner_interrupt = PlannerInterruptController(budget=1)

    first_flag = asyncio.Event()
    runtime._planner_interrupt.bind(first_flag)
    runtime._request_planner_interrupt_for_message(SimpleNamespace(message_id="m1"))
    assert first_flag.is_set() is True

    second_flag = asyncio.Event()
    runtime._planner_interrupt.bind(second_flag)
    runtime._request_planner_interrupt_for_message(SimpleNamespace(message_id="m2"))
    assert second_flag.is_set() is False

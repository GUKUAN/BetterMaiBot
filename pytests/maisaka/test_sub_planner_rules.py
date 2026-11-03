from types import SimpleNamespace

import pytest

from src.maisaka.planner_task import PlannerTaskState
from src.maisaka.sub_planner.rules import decide_by_rules


def _message(**overrides) -> SimpleNamespace:
    base = dict(
        message_id="m1",
        is_at=False,
        is_mentioned=False,
        is_notify=False,
        is_command=False,
        reply_to=None,
        processed_plain_text="今晚八点开服",
        raw_message=SimpleNamespace(components=[]),
        message_info=SimpleNamespace(user_info=SimpleNamespace(user_nickname="老王", user_id="1")),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _task() -> PlannerTaskState:
    task = PlannerTaskState.open(_message(message_id="anchor"))
    task.remember_consumed([_message(message_id="seen")])
    return task


def _decide(message, task, *, budget: bool = True, cooling: bool = False):
    return decide_by_rules(message, task, has_interrupt_budget=budget, cooling_down=cooling)


def test_without_running_task_nothing_interrupts() -> None:
    assert _decide(_message(), None).kind == "continue"


def test_notice_and_command_are_ignored() -> None:
    task = _task()
    assert _decide(_message(is_notify=True), task).kind == "continue"
    assert _decide(_message(is_command=True), task).kind == "continue"


def test_already_consumed_message_is_ignored() -> None:
    assert _decide(_message(message_id="seen"), _task()).kind == "continue"


@pytest.mark.parametrize("field", ["is_at", "is_mentioned"])
def test_being_addressed_interrupts(field: str) -> None:
    assert _decide(_message(**{field: True}), _task()).kind == "interrupt"


def test_replying_to_anchor_interrupts() -> None:
    assert _decide(_message(reply_to="anchor"), _task()).kind == "interrupt"


def test_other_reply_target_does_not_shortcut() -> None:
    # 引用别人不算直接回应主任务，交给模型判断
    assert _decide(_message(reply_to="someone-else"), _task()) is None


def test_tool_running_only_accumulates() -> None:
    task = _task()
    task.enter_tool_phase()
    assert _decide(_message(), task).kind == "update"


def test_exhausted_budget_degrades_to_update() -> None:
    assert _decide(_message(), _task(), budget=False).kind == "update"


def test_cooldown_merges_batch() -> None:
    assert _decide(_message(), _task(), cooling=True).kind == "continue"


def test_bare_emoji_and_image_are_ignored() -> None:
    task = _task()
    emoji = SimpleNamespace(components=[SimpleNamespace(format_name="emoji", text="")])
    image = SimpleNamespace(components=[SimpleNamespace(format_name="image", text="")])

    assert _decide(_message(raw_message=emoji, processed_plain_text=""), task).kind == "continue"
    assert _decide(_message(raw_message=image, processed_plain_text=""), task).kind == "continue"


def test_short_chatter_is_ignored_but_question_goes_to_model() -> None:
    task = _task()
    assert _decide(_message(processed_plain_text="草"), task).kind == "continue"
    # 带问号的短句可能是在追问，交出去判断
    assert _decide(_message(processed_plain_text="啊？"), task) is None


def test_normal_message_is_left_to_model() -> None:
    assert _decide(_message(processed_plain_text="所以那个视频到底讲了啥"), _task()) is None

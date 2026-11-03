from types import SimpleNamespace

from src.maisaka.planner_task import PlannerTaskState
from src.maisaka.sub_planner import decider as decider_module
from src.maisaka.sub_planner.decider import SubPlannerDecider


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


def _config(**overrides) -> SimpleNamespace:
    base = dict(
        enable=True,
        rule_only=False,
        cooldown_seconds=3.0,
        max_decisions_per_task=3,
        timeout_seconds=4.0,
        max_batch_messages=6,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make(monkeypatch, task, *, config=None, budget: bool = True):
    monkeypatch.setattr(decider_module, "global_config", SimpleNamespace(sub_planner=config or _config()))

    interrupts = []

    runtime = SimpleNamespace(
        _planner_task=task,
        _running=True,
        log_prefix="[test]",
        session_id="s1",
        _planner_interrupt=SimpleNamespace(has_budget_left=lambda: budget),
        _request_planner_interrupt_for_message=lambda message: interrupts.append(message.message_id),
    )
    return SubPlannerDecider(runtime), interrupts


def _task() -> PlannerTaskState:
    return PlannerTaskState.open(_message(message_id="anchor"))


def test_disabled_keeps_legacy_direct_interrupt(monkeypatch) -> None:
    decider, interrupts = _make(monkeypatch, _task(), config=_config(enable=False))

    decider.observe(_message(message_id="m2"))

    assert interrupts == ["m2"]


def test_addressing_bot_interrupts(monkeypatch) -> None:
    decider, interrupts = _make(monkeypatch, _task())

    decider.observe(_message(message_id="m2", is_at=True))

    assert interrupts == ["m2"]


def test_short_chatter_is_ignored(monkeypatch) -> None:
    decider, interrupts = _make(monkeypatch, _task())

    decider.observe(_message(message_id="m2", processed_plain_text="草"))

    assert interrupts == []


def test_rule_only_does_not_fall_back_to_interrupt(monkeypatch) -> None:
    """开了只用规则，规则拿不准就不该再打断——否则这个开关等于没开。"""
    decider, interrupts = _make(monkeypatch, _task(), config=_config(rule_only=True))

    decider.observe(_message(message_id="m2", processed_plain_text="所以那个视频到底讲了啥"))

    assert interrupts == []


def test_undecided_message_goes_to_model_batch(monkeypatch) -> None:
    decider, interrupts = _make(monkeypatch, _task())
    queued = []
    monkeypatch.setattr(decider, "_ensure_worker", lambda: queued.append("worker"))

    decider.observe(_message(message_id="m2", processed_plain_text="所以那个视频到底讲了啥"))

    assert interrupts == []
    assert queued == ["worker"]
    assert [message.message_id for message in decider._batch] == ["m2"]


def test_tool_running_records_update_note(monkeypatch) -> None:
    task = _task()
    task.enter_tool_phase()
    decider, interrupts = _make(monkeypatch, task)

    decider.observe(_message(message_id="m2", processed_plain_text="端口改成 8080 了"))

    assert interrupts == []
    notes = task.take_update_notes()
    assert len(notes) == 1
    assert "端口改成 8080" in notes[0]


def test_exhausted_budget_degrades_to_update(monkeypatch) -> None:
    task = _task()
    decider, interrupts = _make(monkeypatch, task, budget=False)

    decider.observe(_message(message_id="m2"))

    assert interrupts == []
    assert task.take_update_notes()


def test_decision_quota_resets_on_new_task(monkeypatch) -> None:
    task = _task()
    decider, _ = _make(monkeypatch, task)
    decider._decided_in_task = 3

    decider._sync_task(PlannerTaskState.open(_message(message_id="another")))

    assert decider._decided_in_task == 0
    assert decider._batch == []


def test_stale_snapshot_is_rejected(monkeypatch) -> None:
    task = _task()
    decider, _ = _make(monkeypatch, task)
    snapshot = (task.task_id, task.version)

    assert decider._is_stale(snapshot) is False
    task.bump()
    assert decider._is_stale(snapshot) is True

    another = PlannerTaskState.open(_message(message_id="x"))
    decider._runtime._planner_task = another
    assert decider._is_stale((task.task_id, task.version)) is True

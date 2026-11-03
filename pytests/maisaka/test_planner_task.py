from types import SimpleNamespace

from src.maisaka.planner_task import PlannerTaskState


def _msg(message_id: str, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(message_id=message_id, processed_plain_text=text)


def test_open_keeps_anchor_and_starts_clean() -> None:
    anchor = _msg("a1")
    task = PlannerTaskState.open(anchor)

    assert task.anchor_message is anchor
    assert task.version == 0
    assert task.phase == "planning"
    assert task.task_id


def test_tool_phase_counts_as_safe_node() -> None:
    task = PlannerTaskState.open(_msg("a1"))

    task.enter_tool_phase()
    assert task.phase == "tool_running"

    task.leave_tool_phase()
    assert task.phase == "planning"
    assert task.version == 1


def test_reasoning_is_truncated() -> None:
    task = PlannerTaskState.open(_msg("a1"))

    task.remember_reasoning("思" * 900)
    assert len(task.last_reasoning) == 400

    task.remember_reasoning(None)
    assert task.last_reasoning == ""


def test_consumed_ids_drive_deduplication() -> None:
    task = PlannerTaskState.open(_msg("a1"))
    assert task.already_consumed("m1") is False

    task.remember_consumed([_msg("m1"), _msg("m2")])
    assert task.already_consumed("m1") is True
    assert task.already_consumed("m3") is False


def test_update_notes_are_drained_once() -> None:
    task = PlannerTaskState.open(_msg("a1"))

    task.note_update("端口改成 8080")
    task.note_update("   ")
    assert task.take_update_notes() == ["端口改成 8080"]
    assert task.take_update_notes() == []


def test_deferred_ids_are_unique() -> None:
    task = PlannerTaskState.open(_msg("a1"))

    task.note_deferred("d1")
    task.note_deferred("d1")
    task.note_deferred("d2")
    assert task.take_deferred() == ["d1", "d2"]
    assert task.take_deferred() == []

"""一次内部思考（turn）的任务快照。

副思考需要知道"主任务正在做什么"才能判断新消息要不要影响它，但主循环里原本只有
_agent_state 这个三态标记和不断变长的 _chat_history。这里把判断所需的最小事实收在一处：
任务身份、版本、阶段、触发消息、最近一轮思考文本、已经看过哪些消息。

version 是给副思考用的作废标记：每到一个安全节点就 +1，副思考拿旧版本提交的结论会被丢掉。
"""

from dataclasses import dataclass, field
from typing import Iterable, List, Literal, Optional, Set

import time
import uuid

from src.chat.message_receive.message import SessionMessage


PlannerPhase = Literal["planning", "tool_running", "finalizing"]

LAST_REASONING_MAX_CHARS = 400
"""快照里保留的最近思考长度。副思考只用来判断话题是否相关，不需要全文。"""


@dataclass(slots=True)
class PlannerTaskState:
    """一条主思考任务的运行态。"""

    task_id: str
    version: int = 0
    phase: PlannerPhase = "planning"
    round_index: int = 0
    started_at: float = field(default_factory=time.time)
    anchor_message: Optional[SessionMessage] = None
    last_reasoning: str = ""
    consumed_message_ids: Set[str] = field(default_factory=set)
    update_notes: List[str] = field(default_factory=list)
    """副思考判为 update 的说明，下一轮请求时并进注入消息。"""
    deferred_message_ids: List[str] = field(default_factory=list)
    """副思考判为 defer 的消息，本轮结束后需要单独开一轮。"""

    @classmethod
    def open(cls, anchor_message: Optional[SessionMessage]) -> "PlannerTaskState":
        """按本轮触发消息开启一条任务。"""

        return cls(task_id=uuid.uuid4().hex[:12], anchor_message=anchor_message)

    def bump(self) -> None:
        """进入安全节点，让在途的副思考结论失效。"""

        self.version += 1

    def enter_tool_phase(self) -> None:
        """工具执行期间不接受打断：中断串行工具会留下做了一半的副作用。"""

        self.phase = "tool_running"

    def leave_tool_phase(self) -> None:
        """工具批结束，回到可以继续规划的状态，同时算作一次安全节点。"""

        self.phase = "planning"
        self.bump()

    def remember_reasoning(self, text: Optional[str]) -> None:
        """记录最近一轮 planner 正文，供副思考判断话题是否还在同一条线上。"""

        self.last_reasoning = (text or "").strip()[:LAST_REASONING_MAX_CHARS]

    def remember_consumed(self, messages: Iterable[SessionMessage]) -> None:
        """记下已经进入主上下文的消息，副思考落地时据此去重。"""

        for message in messages:
            self.consumed_message_ids.add(message.message_id)

    def already_consumed(self, message_id: str) -> bool:
        return message_id in self.consumed_message_ids

    def note_update(self, note: str) -> None:
        text = (note or "").strip()
        if text:
            self.update_notes.append(text)

    def take_update_notes(self) -> List[str]:
        """取走待注入的说明；同一批只注入一次。"""

        notes, self.update_notes = self.update_notes, []
        return notes

    def note_deferred(self, message_id: str) -> None:
        if message_id and message_id not in self.deferred_message_ids:
            self.deferred_message_ids.append(message_id)

    def take_deferred(self) -> List[str]:
        ids, self.deferred_message_ids = self.deferred_message_ids, []
        return ids

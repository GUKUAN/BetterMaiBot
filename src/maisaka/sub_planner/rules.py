"""副思考的规则层：不花 token 就能定下来的情况先在这里挡掉。

模型调用有延迟也有成本，群里大部分消息其实不需要模型来判断——表情包、随口两个字、
工具正在跑、打断额度已经用完，这些按规则就能给出结论，剩下的才值得问一次模型。
"""

from dataclasses import dataclass
from typing import Literal, Optional

from src.chat.debounce.message_adapter import get_message_shape
from src.chat.message_receive.message import SessionMessage

from ..planner_task import PlannerTaskState


SubDecisionKind = Literal["continue", "update", "interrupt", "defer"]

SHORT_REPLY_MAX_CHARS = 2
"""不超过这个长度又没有问号的纯文本，当作随口一句，不值得为它打断正在进行的思考。"""


@dataclass(frozen=True, slots=True)
class SubDecision:
    """副思考对一条消息的结论。"""

    kind: SubDecisionKind
    reason: str
    by_rule: bool = True
    """规则层直接给出，没走模型。"""


def decide_by_rules(
    message: SessionMessage,
    task: Optional[PlannerTaskState],
    *,
    has_interrupt_budget: bool,
    cooling_down: bool,
) -> Optional[SubDecision]:
    """能靠规则定下来的直接返回结论；拿不准的返回 None 交给模型。"""

    if message.is_notify or message.is_command:
        return SubDecision("continue", "通知或命令不参与判定")

    if task is None:
        return SubDecision("continue", "当前没有进行中的主思考")

    if task.already_consumed(message.message_id):
        return SubDecision("continue", "这条消息已经被主思考看过了")

    if message.is_at or message.is_mentioned:
        return SubDecision("interrupt", "被点到名字")

    anchor = task.anchor_message
    if anchor is not None and message.reply_to == anchor.message_id:
        return SubDecision("interrupt", "直接引用了本轮正在处理的那条消息")

    if task.phase == "tool_running":
        # 工具是串行执行的，断在中间会留下做了一半的副作用
        return SubDecision("update", "工具执行中，先攒着不打断")

    if not has_interrupt_budget:
        return SubDecision("update", "本轮打断次数已用尽")

    if cooling_down:
        return SubDecision("continue", "判定冷却中，先合并成一批")

    kind, length = get_message_shape(message)
    text = (message.processed_plain_text or "").strip()
    if kind == 2 and length == 0:
        return SubDecision("continue", "纯表情包")
    if kind == 1 and length == 0:
        return SubDecision("continue", "只有图片没有文字")

    # 长度按实际正文算：文件、视频这类段落在组件里没有文字，正文却是 adapter 转好的描述
    has_question = "?" in text or "？" in text
    if not has_question and len(text) <= SHORT_REPLY_MAX_CHARS:
        return SubDecision("continue", "很短的随口一句")

    return None

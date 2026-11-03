"""副思考判定用的提示词与结果解析。"""

from typing import Optional, Sequence, cast

import json
import re

from src.chat.message_receive.message import SessionMessage

from ..planner_task import PlannerTaskState
from .rules import SubDecision, SubDecisionKind


SYSTEM_PROMPT = (
    "你在判断群里新到的消息要不要影响麦麦（群里的机器人）正在进行的思考。\n"
    "\n"
    "判定档位：\n"
    "- continue：只是补充、随口一句，或者跟当前话题无关。不影响。\n"
    "- update：给了当前任务用得上的新信息，应该让麦麦知道，但不用停下重来。\n"
    "- interrupt：推翻了原来的意图，或者明确在等麦麦回答，必须停下重新判断。\n"
    "- defer：是个跟当前任务无关的新问题，等手头这件事做完再处理。\n"
    "\n"
    "拿不准就选 continue。频繁打断会让麦麦一直想不完，反而一条都回不出去。\n"
    "只输出一个 JSON 对象，不要解释，不要代码块：\n"
    '{"decision":"continue|update|interrupt|defer","reason":"不超过30字"}'
)

MAX_ANCHOR_CHARS = 120
MAX_MESSAGE_CHARS = 200
MAX_REASONING_CHARS = 400

SUB_DECISION_KINDS: tuple[SubDecisionKind, ...] = ("continue", "update", "interrupt", "defer")


def build_prompt(batch: Sequence[SessionMessage], task: PlannerTaskState) -> str:
    """拼出一次判定请求；只带判断所需的最小上下文。"""

    lines = [SYSTEM_PROMPT, "", "【当前任务】"]

    anchor = task.anchor_message
    if anchor is not None:
        lines.append(f"触发消息：{_who(anchor)}：{_clip(_text(anchor), MAX_ANCHOR_CHARS)}")
    else:
        lines.append("触发消息：未知")

    lines.append(f"进度：第 {task.round_index + 1} 轮，阶段={task.phase}")

    reasoning = task.last_reasoning.strip()
    lines.append(f"最近在想的：{_clip(reasoning, MAX_REASONING_CHARS) if reasoning else '（还没输出内容）'}")

    lines.append("")
    lines.append("【新到的消息】")
    for index, message in enumerate(batch, start=1):
        lines.append(f"{index}. {_who(message)}：{_clip(_text(message), MAX_MESSAGE_CHARS)}")

    lines.append("")
    lines.append("只输出 JSON。")
    return "\n".join(lines)


def parse_decision(raw: str) -> Optional[SubDecision]:
    """把模型输出解析成结论；解析不出来返回 None，调用方按不影响处理。"""

    payload = _extract_json(raw)
    if payload is None:
        return None

    kind = str(payload.get("decision") or "").strip().lower()
    if kind not in SUB_DECISION_KINDS:
        return None

    reason = str(payload.get("reason") or "").strip() or "模型判定"
    return SubDecision(kind=cast(SubDecisionKind, kind), reason=reason, by_rule=False)


def _extract_json(raw: str) -> Optional[dict]:
    text = (raw or "").strip()
    if not text:
        return None

    # 模型有时会套一层 ```json 代码块
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None

    try:
        payload = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _who(message: SessionMessage) -> str:
    user_info = getattr(message.message_info, "user_info", None)
    nickname = getattr(user_info, "user_nickname", "") or "某人"
    return str(nickname).strip()


def _text(message: SessionMessage) -> str:
    return (message.processed_plain_text or "").strip().replace("\n", " ")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"

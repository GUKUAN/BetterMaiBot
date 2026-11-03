"""饭票注入块：每轮把点单状态折进 Planner 的 system 项"""

import time
from typing import Any

MARKER_MEAL = "【饭票状态】"


def _recent(history: list[dict], status: str, now: float, within: float) -> dict | None:
    for row in reversed(history):
        if row.get("status") != status:
            continue
        decided = float(row.get("decided_at") or 0)
        if decided and now - decided <= within * 3600:
            return row
    return None


def emotion_tier(history: list[dict], window: int) -> str:
    """按最近几单的答应率决定语气档位：老被拒就蔫一点，老被答应就翘尾巴。"""
    rows = [o for o in history if isinstance(o, dict) and o.get("status") in ("approved", "rejected")]
    recent = rows[-window:] if window > 0 else rows
    if len(recent) < 3:
        return "normal"
    approved = sum(1 for o in recent if o.get("status") == "approved")
    rate = approved / len(recent)
    if rate >= 0.7:
        return "confident"
    if rate <= 0.3:
        return "down"
    return "normal"


def build_block(state: dict, currency: str, window: int, now: float) -> str:
    """拼给 Planner 看的点单状态块。state 就是 MealStore.data。"""
    tier = emotion_tier(state.get("history") or [], window)
    mood = {
        "confident": "（最近点单基本都成功，你底气挺足）",
        "down": "（最近老被拒，你有点蔫，说话别太理直气壮）",
        "normal": "",
    }.get(tier, "")

    lines = [MARKER_MEAL, "以下是你自己的点单进度，只做背景，不要主动念出来："]

    pending = state.get("pending")
    if isinstance(pending, dict) and float(pending.get("expires_at") or 0) > now:
        lines.append(
            f"- 你刚跟主人说想吃{pending.get('dish')}（{currency}{pending.get('price')}），"
            "在等他回话。他答应了你就说去点，拒绝了你就换个更便宜的再问一次（只问一次）。"
        )
    current = state.get("current")
    if isinstance(current, dict):
        stage = str(current.get("stage") or "")
        if stage == "delivering":
            left = (float(current.get("arrive_at") or 0) - now) / 60
            lines.append(f"- {current.get('dish')}已经在路上了，约 {left:.0f} 分钟后到，到时候说一声去拿")
        elif stage == "eating":
            left = (float(current.get("done_at") or 0) - now) / 60
            lines.append(f"- 你正在吃{current.get('dish')}，约 {left:.0f} 分钟后吃完，到时候提一句")

    return "\n".join(lines) + mood


def system_item(text: str) -> dict:
    """新建一条独立的 SystemMessageItem。"""
    import uuid
    from datetime import datetime as dt

    return {
        "item_type": "SystemMessageItem",
        "meta": {
            "item_id": uuid.uuid4().hex,
            "logical_turn_id": None,
            "timestamp": dt.now().isoformat(),
        },
        "parts": [{"type": "text", "text": text}],
    }


def upsert_system_block(kwargs: dict, block: str) -> bool:
    """把注入块合并进首个 system 项，没有就追加一条。

    优先合并而不是新起一条，是因为宿主本来就有一条固定人设 system，
    塞进去语义上受上面约束，也不会让请求里出现两条 system。

    幂等靠 marker：先按 MARKER 把该 part 里上一次的注入切掉再拼新的，
    等于覆盖写，宿主重试 N 次也只有一份。

    必须整体替换 kwargs["items"]：只回 {"items": ...} 会丢掉 item_schema_version，
    宿主反序列化失败，整份注入被静默忽略。
    """
    items = kwargs.get("items")
    if not isinstance(items, list) or not items:
        return False

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if "system" not in str(item.get("item_type") or "").lower():
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for pidx in range(len(parts) - 1, -1, -1):
            part = parts[pidx]
            if not isinstance(part, dict) or str(part.get("type", "")).lower() != "text":
                continue
            old = part.get("text") if isinstance(part.get("text"), str) else ""
            head = old.split(MARKER_MEAL, 1)[0].rstrip()
            fresh = list(parts)
            fresh[pidx] = {**part, "text": f"{head}\n\n{block}" if head else block}
            new_item = dict(item)
            new_item["parts"] = fresh
            new_items = list(items)
            new_items[index] = new_item
            kwargs["items"] = new_items
            return True

    kwargs["items"] = list(items) + [system_item(block)]
    return True

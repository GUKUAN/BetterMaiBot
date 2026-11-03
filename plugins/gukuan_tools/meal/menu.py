"""菜单与判定：抽菜、认主人的「买/不买」、听出喊饿"""

import random
import time
from typing import Any, NamedTuple

HUNGRY_WORDS = (
    "饿了", "想吃", "点个外卖", "吃什么", "吃啥", "好饿", "肚子饿",
    "外卖", "干饭", "觅食", "买吃的",
)


class Dish(NamedTuple):
    category: str
    shop: str
    name: str
    low: int
    high: int


def parse_menu(pool: Any) -> list[Dish]:
    """「分类|店名|菜名|最低价|最高价」一行一个，坏行跳过不炸。"""
    dishes: list[Dish] = []
    if not isinstance(pool, list):
        return dishes
    for raw in pool:
        parts = str(raw or "").split("|")
        if len(parts) != 5:
            continue
        try:
            low, high = int(parts[3]), int(parts[4])
        except ValueError:
            continue
        if low > high:
            low, high = high, low
        dishes.append(Dish(
            category=parts[0].strip(), shop=parts[1].strip(),
            name=parts[2].strip(), low=low, high=high,
        ))
    return dishes


def pick_dish(dishes: list[Dish], rng: random.Random, max_price: int | None = None) -> tuple[Dish, int] | None:
    """随机抽一个，价签在区间里随机；有价格上限就只看够得着的。"""
    pool = dishes
    if max_price is not None:
        pool = [d for d in dishes if d.low <= max_price]
        if not pool:
            return None
    dish = rng.choice(pool)
    if max_price is not None:
        top = min(dish.high, max_price)
        price = rng.randint(dish.low, max(dish.low, top))
    else:
        price = rng.randint(dish.low, dish.high)
    return dish, price


def decide_reply(text: str) -> str:
    """主人在等她确认的时候回了话，判断是答应还是拒绝。

    返回 "approve" / "reject" / ""（认不出来就空着，继续等）。
    词表故意写得粗 —— 「行」「好」「嗯」这类单字答应太多样，
    与其堆词不如抓住明确的否定和明确的同意两头。
    """
    text = text.strip()
    if not text:
        return ""

    reject_words = ("不买", "不点", "算了", "不吃", "不要", "拒绝", "不行的", "没钱",
                    "下次", "改天", "再说", "减完肥", "在减肥", "不饿")
    for word in reject_words:
        if word in text:
            return "reject"

    approve_words = ("买吧", "买", "点吧", "点", "可以", "行", "好", "答应",
                     "批准", "同意", "嗯", "哦", "吃吧", "吃", "安排")
    for word in approve_words:
        if word in text:
            return "approve"
    return ""


def clock_text(now: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(now))

"""饭票管理器：饭点调度、起单、判话、推进、注入，一条龙

并发纪律：起单全程串在 _order_lock 里 —— 这是防并发出现两个 pending
的唯一可靠手段。别的操作（判话、推进）都是先改内存再丢后台落盘。
"""

import asyncio
import random
import time
from datetime import datetime, timedelta
from typing import Any

from .blocks import MARKER_MEAL, build_block, upsert_system_block
from .menu import HUNGRY_WORDS, Dish, decide_reply, parse_menu, pick_dish
from .phrases import PHRASE_FALLBACK, PHRASE_PROMPT
from .store import MealStore


class MealManager:
    """饭票。on_message 是钩子入口，inject_hook 是 planner 前的注入入口。"""

    def __init__(self, core: Any) -> None:
        self.core = core
        self.name = "meal"
        self._store: MealStore | None = None
        self._dishes: list[Dish] = []
        self._wake: asyncio.Event | None = None
        self._meal_task: asyncio.Task | None = None
        self._order_lock = asyncio.Lock()
        self._rng = random.Random()

    # ---------------------------------------------------------- 生命周期

    async def setup(self) -> None:
        self._store = MealStore(self.core.data_dir, self.core.logger)
        self._store.load(default_enabled=bool(self.core.config.meal.active_enabled))
        self._dishes = parse_menu(self.core.config.menu.pool)
        self._wake = asyncio.Event()
        self._meal_task = asyncio.get_running_loop().create_task(self._meal_loop())
        self.core.spawn(self._resume())

    async def teardown(self) -> None:
        task, self._meal_task = self._meal_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            if self._store is not None:
                self._store.save_sync()
        except OSError:
            self.core.logger.debug("饭票：卸载时落盘失败")

    def reload(self) -> None:
        self._dishes = parse_menu(self.core.config.menu.pool)
        if self._wake is not None:
            self._wake.set()  # 唤醒调度重算饭点

    @property
    def store(self) -> MealStore | None:
        return self._store

    def status_line(self) -> str:
        """on_load 时打给日志的一句话。"""
        cfg = self.core.config
        return (
            f"菜品 {len(self._dishes)} 个，"
            f"饭票={'开' if cfg.meal.enabled else '关'}，"
            f"主动喊饿={'开' if (self._store and self._store.enabled) else '关'}"
        )

    # ---------------------------------------------------------- 消息入口

    def on_message(self, message: dict | None) -> None:
        if not self.core.config.plugin.enabled or not self.core.config.meal.enabled:
            return
        if not isinstance(message, dict) or self._store is None:
            return
        info = message.get("message_info") or {}
        user = info.get("user_info") or {}
        if str(user.get("user_id") or "") != str(self.core.config.target.owner_qq):
            return  # 只跟主人点单
        group_id = str((info.get("group_info") or {}).get("group_id") or "")
        if group_id and not self.core.config.meal.allow_group:
            return
        text = str(message.get("processed_plain_text") or "").strip()
        if not text or text.startswith("/"):
            return

        now = time.time()

        # 一、有单在等他回话，先判买不买
        if self._store.pending_alive(now):
            verdict = decide_reply(text)
            if verdict:
                self._verdict(verdict, now)
                return

        # 二、随口喊饿
        if not self._store.enabled or self._order_lock.locked():
            return
        if not any(w in text for w in HUNGRY_WORDS):
            return
        if now - self._store.last_ask_at < self.core.config.meal.cooldown_min * 60:
            return
        if self._rng.random() > self.core.config.meal.chance_in_chat:
            return
        self.core.logger.info(f"饭票：主人喊饿了（{text[:20]}），后台起一单")
        self.core.spawn(self.order("chat"))

    # ---------------------------------------------------------- 判话与推进

    def _verdict(self, verdict: str, now: float) -> None:
        """同步改状态再丢后台存盘——防同一秒两条消息把一单判两次。"""
        if self._store is None:
            return
        status = "approved" if verdict == "approve" else "rejected"
        order = self._store.close_pending(status, now)
        if not order:
            return
        self.core.logger.info(
            f"饭票：主人{'答应' if verdict == 'approve' else '拒绝'}了 "
            f"{order.get('dish')}（{order.get('price')}，单号={order.get('id')}）"
        )
        self.core.spawn(self._after_verdict(order, status))

    async def _after_verdict(self, order: dict, status: str) -> None:
        if self._store is None:
            return
        if status == "approved":
            now = time.time()
            arrive = now + int(self.core.config.meal.arrive_min) * 60
            done = arrive + int(self.core.config.meal.eat_min) * 60
            self._store.start_flight(order, arrive, done)
            await self._store.save()
            self.core.logger.info(f"饭票：{order.get('dish')} 已下单，{self.core.config.meal.arrive_min} 分钟后到")
            self.core.spawn(self.core.later(arrive - now, self._say_arrived, order))
            return

        await self._store.save()
        if not self.core.config.meal.retry_once or int(order.get("attempt") or 1) >= 2:
            self.core.logger.info("饭票：这单算了，不再问")
            return
        low = min(self.core.config.meal.retry_delay_min, self.core.config.meal.retry_delay_max_min)
        high = max(self.core.config.meal.retry_delay_min, self.core.config.meal.retry_delay_max_min)
        delay = self._rng.randint(low, high) * 60
        self.core.logger.info(f"饭票：{delay // 60} 分钟后换个便宜的再问一次")
        self.core.spawn(self.core.later(delay, self._retry, order))

    async def _retry(self, order: dict) -> None:
        await self.order("retry", parent=order)

    # ---------------------------------------------------------- 起单

    async def order(self, reason: str, parent: dict | None = None, force: bool = False) -> None:
        """起一单。整套「查状态 → 抽菜 → 生成话术 → 发送 → 落盘」串在一把锁里，
        这是防并发出现两个 pending 的唯一可靠手段。
        """
        if self._store is None:
            return
        async with self._order_lock:
            now = time.time()
            if not self.core.config.plugin.enabled or not self.core.config.meal.enabled:
                return
            if not force and not self._store.enabled:
                return

            if self._store.pending_alive(now):
                self.core.logger.info(f"饭票：还有单在等他回话，跳过（{reason}）")
                return
            if self._store.current():
                self.core.logger.info(f"饭票：上一单还没吃完，跳过（{reason}）")
                return

            expired = self._store.expire_pending(now)
            if expired:
                self.core.logger.info(f"饭票：{expired.get('dish')} 那单主人没回，作废")

            if not force:
                if now - self._store.last_ask_at < self.core.config.meal.cooldown_min * 60:
                    self.core.logger.info(f"饭票：离上一单不到 {self.core.config.meal.cooldown_min} 分钟，跳过")
                    return
                if self._store.day_left(now, self.core.config.meal.daily_max) <= 0:
                    self.core.logger.info("饭票：今天开口次数用完了")
                    return
                skip = self.core.life_skip(now)
                if skip:
                    self.core.logger.info(f"饭票：这次不开口（{skip}）")
                    return

            if not self._dishes:
                self.core.logger.warning("饭票：菜单是空的，去 config.toml 的 menu.pool 里填几行")
                return

            attempt, parent_id, max_price = 1, None, None
            if parent:
                attempt = 2
                parent_id = str(parent.get("id") or "") or None
                max_price = max(1, int(int(parent.get("price") or 0) * self.core.config.meal.retry_price_ratio))

            picked = pick_dish(self._dishes, self._rng, max_price)
            if picked is None:
                return
            dish, price = picked

            line = await self.core.phrase("ask", dish, price, PHRASE_PROMPT, PHRASE_FALLBACK)
            ok, sid = await self.core.send_to_owner(line)
            if not ok:
                self.core.logger.warning(f"饭票：私信没发出去，这单作废（{dish.name} {price}）")
                return

            row = self._store.open_pending(
                dish, price, time.time(), self.core.config.meal.pending_ttl_min, attempt, parent_id
            )
            self._store.bump_day()
            await self._store.save()
            self.core.logger.info(
                f"饭票：起单 {dish.name} {self.core.config.money.currency}{price}"
                f"（{reason}，第{attempt}次，单号={row['id']}）"
            )

            await self.core.note(
                f"你刚跟主人说想吃{dish.name}"
                f"（{self.core.config.money.currency}{price}，来自{dish.shop}）。"
                "你在等他回话，别重复开口，也别催。",
                sid,
            )

    # ---------------------------------------------------------- 推进

    async def _say_arrived(self, order: dict) -> None:
        if self._store is None:
            return
        dish = self._as_dish(order)
        line = await self.core.phrase("arrived", dish, int(order.get("price") or 0), PHRASE_PROMPT, PHRASE_FALLBACK)
        ok, sid = await self.core.send_to_owner(line)
        if not ok:
            self.core.logger.warning(f"饭票：{dish.name} 的到餐消息没发出去，叙事跳过这一句")
        else:
            await self.core.note(f"（点单）{dish.name} 送到了，你下去拿了上来。", sid)

        self._store.advance("eating")
        await self._store.save()
        left = float(order.get("done_at") or 0) - time.time()
        self.core.spawn(self.core.later(left, self._say_done, order))

    async def _say_done(self, order: dict) -> None:
        if self._store is None:
            return
        dish = self._as_dish(order)
        line = await self.core.phrase("done", dish, int(order.get("price") or 0), PHRASE_PROMPT, PHRASE_FALLBACK)
        ok, sid = await self.core.send_to_owner(line)
        if not ok:
            self.core.logger.warning(f"饭票：{dish.name} 的吃完消息没发出去")
        else:
            await self.core.note(f"（点单）{dish.name} 吃完了，吃得很饱。", sid)

        self._store.finish(time.time())
        await self._store.save()
        self.core.logger.info(f"饭票：{dish.name} 吃完了，这单结了")

    async def _resume(self) -> None:
        """reload 之后把没说完的话补上，别让一单送到一半断掉。"""
        if self._store is None:
            return
        order = self._store.current()
        if not order:
            return
        now = time.time()
        stage = str(order.get("stage") or "")
        if stage == "delivering":
            left = float(order.get("arrive_at") or 0) - now
            if left <= 0:
                self.core.logger.info("饭票：重启期间餐已经到了，补一句")
                await self._say_arrived(order)
            else:
                self.core.logger.info(f"饭票：接上没送完的单，{left / 60:.0f} 分钟后到")
                self.core.spawn(self.core.later(left, self._say_arrived, order))
        elif stage == "eating":
            left = float(order.get("done_at") or 0) - now
            if left <= 0:
                self.core.logger.info("饭票：重启期间已经吃完了，补一句")
                await self._say_done(order)
            else:
                self.core.spawn(self.core.later(left, self._say_done, order))
        else:
            self.core.logger.info(f"饭票：有单卡在奇怪的状态 {stage!r}，直接结掉")
            self._store.finish(now)
            await self._store.save()

    def _as_dish(self, order: dict) -> Dish:
        price = int(order.get("price") or 0)
        return Dish(
            category=str(order.get("category") or ""),
            shop=str(order.get("shop") or "楼下那家"),
            name=str(order.get("dish") or "外卖"),
            low=price, high=price,
        )

    # ---------------------------------------------------------- 调度

    def _next_meal(self) -> datetime:
        """算下一个饭点：今天最近的未来时刻，全过了就顺延到明天。"""
        now = datetime.now()
        best: datetime | None = None
        for raw in self.core.config.meal.meal_times:
            try:
                hour, minute = (int(x) for x in str(raw).strip().split(":", 1))
            except (TypeError, ValueError):
                self.core.logger.warning(f"饭票：meal_times 里的 {raw!r} 看不懂，跳过")
                continue
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                self.core.logger.warning(f"饭票：meal_times 里的 {raw!r} 越界，跳过")
                continue
            point = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if point <= now:
                point += timedelta(days=1)
            if best is None or point < best:
                best = point
        if best is None:
            self.core.logger.warning("饭票：饭点全配错了，退化成每小时看一次")
            return now + timedelta(hours=1)
        return best

    async def _meal_loop(self) -> None:
        """睡到下一个饭点 -> 掷骰子 -> 随机延迟 -> 起单 -> 重算。"""
        while True:
            try:
                target = self._next_meal()
                wait = max(0.0, (target - datetime.now()).total_seconds())
                self.core.logger.info(f"饭票：下次饭点 {target.strftime('%m-%d %H:%M')}（{wait:.0f} 秒后）")
                wake = self._wake
                try:
                    await asyncio.wait_for(wake.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass  # 到点了
                else:
                    self._wake.clear()
                    self.core.logger.info("饭票：配置变了，重算饭点")
                    continue
            except asyncio.CancelledError:
                self.core.logger.info("饭票：调度停了")
                raise
            except Exception:
                self.core.logger.exception("饭票：调度等待出错，60 秒后继续")
                await asyncio.sleep(60)
                continue

            try:
                await self._fire_meal()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.core.logger.exception("饭票：饭点触发出错，等下一个饭点")

    async def _fire_meal(self) -> None:
        if not self.core.config.plugin.enabled or not self.core.config.meal.enabled:
            return
        if self._store is not None and not self._store.enabled:
            return
        if self._rng.random() > self.core.config.meal.probability:
            self.core.logger.info("饭票：这次饭点不开口（概率没中）")
            return
        if self.core.config.meal.delay_max_min > 0:
            delay = self._rng.uniform(0.0, self.core.config.meal.delay_max_min * 60)
            self.core.logger.info(f"饭票：{delay:.0f} 秒后开口")
            await asyncio.sleep(delay)
        await self.order("meal")

    # ---------------------------------------------------------- 注入

    async def inject_hook(self, **kwargs: Any) -> dict[str, Any]:
        """每轮把点单状态折进 Planner 的首个 system 项。"""
        if not self.core.config.plugin.enabled or not self.core.config.meal.enabled:
            return {"action": "continue"}
        if self._store is None:
            return {"action": "continue"}

        session_id = str(kwargs.get("session_id") or "")
        stream_id = self.core.owner_stream_id
        if stream_id and session_id and session_id != stream_id:
            # 已知目标私聊时只注入那条会话，群里没必要让所有人看见她在要饭
            return {"action": "continue"}

        try:
            block = build_block(
                self._store.data,
                self.core.config.money.currency,
                self.core.config.meal.emotion_window,
                time.time(),
            )
        except Exception as exc:
            self.core.logger.debug(f"饭票：拼注入文本出错: {exc}")
            return {"action": "continue"}

        payload = dict(kwargs)
        if not upsert_system_block(payload, block):
            self.core.logger.debug("饭票：items 形态不匹配，本轮不注入")
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": payload}

    # ---------------------------------------------------------- 命令逻辑

    async def cmd_feed(self, kwargs: dict) -> None:
        if not self.core.config.meal.enabled:
            await self.core.reply_command(kwargs, "饭票是关着的呀")
            return
        if not self._dishes:
            await self.core.reply_command(kwargs, "菜单是空的，先去 config.toml 的 menu.pool 里填几行")
            return
        if self._store is not None and self._store.pending_alive(time.time()):
            await self.core.reply_command(kwargs, "还有一单在等你回话呢，先把那个说了")
            return
        self.core.spawn(self.order("manual", force=True))
        await self.core.reply_command(kwargs, "唔…让我想想吃什么")

    async def cmd_ledger(self, kwargs: dict, period: str) -> None:
        if self._store is None:
            await self.core.reply_command(kwargs, "账本还没打开")
            return
        now = time.time()
        if period == "上月":
            now -= 32 * 86400
        elif period == "上周":
            now -= 7 * 86400
        key = "week" if "周" in period else "month"
        stats = self._store.summary(now, key)
        cur = self.core.config.money.currency
        budget = int(self.core.config.money.month_budget)
        tail = ""
        if key == "month" and budget > 0:
            tail = f"，预算还剩{cur}{max(0, budget - stats['spent'])}"
        await self.core.reply_command(kwargs, (
            f"{period}：点了 {stats['count']} 次，答应 {stats['approved']} 次、"
            f"拒绝 {stats['rejected']} 次，花了{cur}{stats['spent']}{tail}"
        ))

    async def cmd_history(self, kwargs: dict, count: int) -> None:
        if self._store is None:
            await self.core.reply_command(kwargs, "还没有单呢")
            return
        rows = self._store.recent(min(count, 30))
        if not rows:
            await self.core.reply_command(kwargs, "还没有吃过一顿呢")
            return
        label = {"approved": "答应", "rejected": "拒绝", "expired": "没回"}
        cur = self.core.config.money.currency
        lines = ["最近这几单："]
        for row in rows:
            when = datetime.fromtimestamp(float(row.get("decided_at") or row.get("created_at") or 0))
            lines.append(
                f"{when.strftime('%m-%d %H:%M')} {label.get(str(row.get('status')), '?')} "
                f"{row.get('dish')} {cur}{row.get('price')}"
            )
        await self.core.reply_command(kwargs, "\n".join(lines))

    async def cmd_toggle(self, kwargs: dict, action: str) -> None:
        if self._store is None:
            await self.core.reply_command(kwargs, "饭票还没准备好")
            return
        if action in ("开", "on"):
            self._store.set_enabled(True)
        elif action in ("关", "off"):
            self._store.set_enabled(False)
        else:
            self._store.set_enabled(not self._store.enabled)
        self.core.spawn(self._store.save())
        await self.core.reply_command(kwargs, f"主动喊饿：{'开' if self._store.enabled else '关'}")

"""新梗管理器：采集调度、原生挖词旁听、玩梗注入、全部梗指令的业务逻辑"""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any

from .collector import (
    MARKER_PLAY, lookup as collector_lookup,
    play_block, push_one as collector_push_one, run_round as collector_run_round,
    upsert_item,
)
from .store import (
    add_pending as store_add_pending, add_play as store_add_play,
    bump_play as store_bump_play, init_db, list_pending, mark_term,
    next_play, stats as store_stats, take_pending,
)


class GengManager:
    """新梗。db_path 在 setup 时定，collector 和 store 都是纯函数库。"""

    def __init__(self, core: Any) -> None:
        self.core = core
        self.name = "geng"
        self._db = ""
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._busy = False
        self._seen: set[str] = set()
        self._play_at = 0.0
        self._play_count = 0
        self._play_today = ""

    async def setup(self) -> None:
        self._db = self.core.data_dir + "/geng.db"
        init_db(self._db)
        self._wake = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._loop())
        self.core.logger.info(f"新梗：就绪（{'开' if self.core.config.geng.enabled else '关'}，每天 {self.core.config.geng.run_at} 采集）")

    async def teardown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def reload(self) -> None:
        if self._wake is not None:
            self._wake.set()  # 采集时间可能也改了

    # ---------------------------------------------------------- 调度

    def _next_run(self) -> datetime:
        """算下一次采集时间，脏配置就退回 04:30。"""
        now = datetime.now()
        raw = str(self.core.config.geng.run_at).strip()
        try:
            hour, minute = (int(x) for x in raw.split(":", 1))
        except (TypeError, ValueError):
            self.core.logger.warning(f"梗：run_at 写得看不懂 {raw!r}，按 04:30 算")
            hour, minute = 4, 30
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            self.core.logger.warning(f"梗：run_at 越界 {raw!r}，按 04:30 算")
            hour, minute = 4, 30
        point = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if point <= now:
            point += timedelta(days=1)
        return point

    async def _loop(self) -> None:
        """睡到点 -> 跑一轮 -> 重算。跟饭票一个套路，省心。"""
        while True:
            try:
                target = self._next_run()
                wait = max(0.0, (target - datetime.now()).total_seconds())
                self.core.logger.info(f"梗：下次采集 {target.strftime('%m-%d %H:%M')}（{wait:.0f} 秒后）")
                wake = self._wake
                try:
                    await asyncio.wait_for(wake.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass  # 到点了
                else:
                    wake.clear()
                    self.core.logger.info("梗：配置变了，重算采集时间")
                    continue
            except asyncio.CancelledError:
                self.core.logger.info("梗：采集调度停了")
                raise
            except Exception:
                self.core.logger.exception("梗：调度等待出错，60 秒后继续")
                await asyncio.sleep(60)
                continue

            try:
                await self.run_round("daily")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.core.logger.exception("梗：这轮采集炸了，等下一轮")

    async def run_round(self, mode: str, limit: int = 0) -> dict:
        """跑一轮。冷启动要跑很久，别让它跟日常那轮撞上。"""
        if not self.core.config.geng.enabled:
            return {}
        if self._busy:
            self.core.logger.info("梗：上一轮还没跑完，这轮跳过")
            return {}
        self._busy = True
        try:
            return await collector_run_round(
                self.core.config, self.core.ctx, self.core.logger,
                db_path=self._db, mode=mode, limit=limit,
            )
        finally:
            self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    # ---------------------------------------------------------- hook 逻辑

    async def on_jargon_entries(self, entries: list | None) -> dict[str, Any]:
        """原生挖词旁听：只看不改。原生挖出来的词大多没有释义，攒起来下一轮补上。

        entries 的改写权留给宿主自己，这里动它会跟 jargon_miner 打架。
        """
        if not self.core.config.geng.enabled or not isinstance(entries, list):
            return {"action": "continue"}
        for entry in entries[:10]:
            if not isinstance(entry, dict):
                continue
            term = str(entry.get("content") or "").strip()
            if 2 <= len(term) <= 12:
                self.core.spawn(self._note(term))
        return {"action": "continue"}

    async def _note(self, term: str) -> None:
        """记进待审队列，标成 from_ai，明天采集时优先补。

        原生挖词触发很频繁，先在内存里挡一道，别每次都全表翻 200 条。
        """
        if term in self._seen:
            return
        self._seen.add(term)
        try:
            rows = await asyncio.to_thread(list_pending, self._db, 200)
            if any(row.get("term") == term for row in rows):
                return
            await asyncio.to_thread(store_add_pending, self._db, term, "", 0.3, 0, "maibot", True)
        except Exception as exc:
            self.core.logger.debug(f"梗：记候选出错 {exc}")

    def _play_ok(self, session_id: str) -> bool:
        """能不能抛。会话过滤 -> 冷却 -> 每日额度，一道比一道严。"""
        cfg = self.core.config.gengplay
        watch = cfg.watch_groups
        if watch and not any(str(w) in session_id for w in watch):
            return False
        if not cfg.allow_private and not session_id:
            return False
        if time.time() - self._play_at < float(cfg.cooldown_min) * 60:
            return False
        today = datetime.now().strftime("%Y-%m-%d")
        if self._play_today != today:
            self._play_today = today
            self._play_count = 0
        return self._play_count < int(cfg.daily_max)

    async def play_hook(self, **kwargs: Any) -> dict[str, Any]:
        """有合适的梗就递一句，用不用让她自己判断。"""
        if not self.core.config.geng.enabled or not self.core.config.gengplay.enabled:
            return {"action": "continue"}
        if not self._play_ok(str(kwargs.get("session_id") or "")):
            return {"action": "continue"}
        if self.core.config.gengplay.chance < 1.0:
            import random
            if random.random() > float(self.core.config.gengplay.chance):
                return {"action": "continue"}

        row = await asyncio.to_thread(self._next_to_play)
        if not row:
            return {"action": "continue"}

        # 独立 system 项，不碰饭票那块，两个块各留各的 MARKER
        payload = dict(kwargs)
        if not upsert_item(payload, play_block(row), MARKER_PLAY):
            return {"action": "continue"}
        self._mark_played(row["term"])
        self.core.logger.info(f"梗：这轮递了「{row['term']}」（已抛 {row['played'] + 1} 次）")
        return {"action": "continue", "modified_kwargs": payload}

    def _next_to_play(self) -> dict | None:
        """只挑近期采到的梗 —— 一个月前的词硬塞出来太像复读机了。"""
        days = int(self.core.config.gengplay.max_age_days)
        since = time.time() - days * 86400 if days > 0 else 0.0
        return next_play(self._db, self.core.config.gengplay.min_fun,
                         self.core.config.gengplay.max_times, since)

    def _mark_played(self, term: str) -> None:
        self._play_at = time.time()
        self._play_count += 1
        self.core.spawn(asyncio.to_thread(store_bump_play, self._db, term))

    async def tool_lookup(self, term: str) -> dict:
        """cha_geng 工具的查询链。"""
        card = await collector_lookup(self.core.config, term, self.core.ctx, self.core.logger)
        if not card:
            return {"success": False,
                    "content": f"（「{term}」没查到确定说法，可能是新词或写法不一样，按你自己的理解讲就行）"}
        return {
            "success": True,
            "content": (
                f"「{card['term']}」：{card['meaning']}"
                f"（把握 {card['confidence']:.0%}，来源 {card['source']}）"
            ),
        }

    # ---------------------------------------------------------- 指令逻辑

    async def cmd_stats(self, kwargs: dict) -> None:
        days = int(self.core.config.gengplay.max_age_days)
        since = time.time() - days * 86400 if days > 0 else 0.0
        data = await asyncio.to_thread(
            store_stats, self._db, int(self.core.config.gengplay.max_times), since
        )
        busy = "（正在跑）" if self._busy else ""
        await self.core.reply_command(kwargs, (
            f"标题池 {data['titles']} 条｜入过库 {data['pushed']} 条（今天 {data['today']}）"
            f"｜待审 {data['pending']}｜待玩 {data['play']}{busy}"
        ))

    async def cmd_run(self, kwargs: dict) -> None:
        if self._busy:
            await self.core.reply_command(kwargs, "上一轮还在跑，等它跑完")
            return
        self.core.spawn(self.run_round("daily"))
        await self.core.reply_command(kwargs, "开始捞了，跑完看日志")

    async def cmd_cold(self, kwargs: dict, want: int) -> None:
        if self._busy:
            await self.core.reply_command(kwargs, "上一轮还在跑，等它跑完")
            return
        want = max(1, min(int(want), 200))
        self.core.spawn(self.run_round("cold", want))
        await self.core.reply_command(kwargs, f"冷启动跑起来了，这轮目标 {want} 条（萌百限速，慢是正常的）")

    async def cmd_lookup(self, kwargs: dict, term: str) -> None:
        if not term:
            await self.core.reply_command(kwargs, "要查哪个词呀")
            return
        card = await collector_lookup(self.core.config, term, self.core.ctx, self.core.logger)
        if not card:
            await self.core.reply_command(kwargs, f"「{term}」这次没弄出来——萌百可能没这条，搜索也没给确定说法")
            return
        await self.core.reply_command(kwargs, f"「{card['term']}」{card['meaning']}")

    async def cmd_pending(self, kwargs: dict) -> None:
        rows = await asyncio.to_thread(list_pending, self._db, 10)
        if not rows:
            await self.core.reply_command(kwargs, "待审队列是空的")
            return
        lines = ["待审（/梗收 编号 收下，/梗弃 编号 丢掉）："]
        for row in rows:
            mark = "原生" if row.get("from_ai") else (row.get("source") or "?")
            body = row["meaning"][:28] or "（还没释义）"
            lines.append(f"{row['id']}. {row['term']}｜{body}｜{row['confidence']:.0%}｜{mark}")
        await self.core.reply_command(kwargs, "\n".join(lines))

    async def cmd_take(self, kwargs: dict, pid: int) -> None:
        row = await asyncio.to_thread(take_pending, self._db, int(pid))
        if not row:
            await self.core.reply_command(kwargs, "没这个编号")
            return
        if not row["meaning"]:
            await self.core.reply_command(kwargs, f"「{row['term']}」还没有释义，先 /梗找 {row['term']}")
            return
        ok = await collector_push_one(
            self.core.config, self.core.ctx, self.core.logger, self._db,
            row["term"], row["meaning"], row["confidence"], row["fun"], row["source"],
        )
        await self.core.reply_command(kwargs, ("收下了，她以后看到这词就懂了" if ok else "写库没成功，看日志"))

    async def cmd_drop(self, kwargs: dict, pid: int) -> None:
        row = await asyncio.to_thread(take_pending, self._db, int(pid))
        if not row:
            await self.core.reply_command(kwargs, "没这个编号")
            return
        await asyncio.to_thread(
            mark_term, self._db, row["term"], row["meaning"], row["confidence"],
            row["fun"], row["source"], "dropped",
        )
        await self.core.reply_command(kwargs, f"「{row['term']}」丢掉了")

    async def cmd_play(self, kwargs: dict, term: str) -> None:
        if not term:
            row = await asyncio.to_thread(self._next_to_play)
            if not row:
                await self.core.reply_command(kwargs, "待玩队列空着，先攒点梗吧")
                return
            await self.core.reply_command(kwargs, f"下一条想抛的是「{row['term']}」，已经抛过 {row['played']} 次")
            return

        rows = await asyncio.to_thread(list_pending, self._db, 200)
        meaning = next((r["meaning"] for r in rows if r["term"] == term and r["meaning"]), "")
        if not meaning:
            card = await collector_lookup(self.core.config, term, self.core.ctx, self.core.logger)
            meaning = card["meaning"] if card else ""
        if not meaning:
            await self.core.reply_command(kwargs, f"「{term}」查不到意思，没法玩")
            return
        await asyncio.to_thread(store_add_play, self._db, term, meaning, 5)
        await self.core.reply_command(kwargs, f"「{term}」排进待玩队列了，下一轮她看情况会用")

    async def cmd_toggle(self, kwargs: dict, action: str) -> None:
        on = self.core.config.geng.enabled
        if action in ("开", "on"):
            on = True
        elif action in ("关", "off"):
            on = False
        else:
            on = not on
        try:
            self.core.config.geng.enabled = on
        except Exception:
            await self.core.reply_command(kwargs, "这份配置是只读的，去 config.toml 改 geng.enabled 再 reload")
            return
        await self.core.reply_command(kwargs, (
            f"新梗：{'开' if on else '关'}（只改本次运行，要长期生效去 config.toml 改 geng.enabled）"
        ))

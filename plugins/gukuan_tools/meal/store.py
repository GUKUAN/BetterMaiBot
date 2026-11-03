"""饭票的持久层：state.json 的唯一读写入口

单写者（一个插件进程一个 Store）+ 原子替换（tmp -> fsync -> os.replace），
断电也不会留下半截 JSON。
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4


def _blank(enabled: bool = True) -> dict:
    return {
        "schema": 1,
        "pending": None,
        "current": None,
        "history": [],
        "meta": {"last_ask_at": 0.0, "enabled": bool(enabled), "day_key": "", "day_count": 0},
    }


def _period_start(now: float, period: str) -> float:
    """算本周/本月的起点。时间戳离谱（0 或未来太远）时退回 0，别让统计把插件搞崩。"""
    try:
        dt = datetime.fromtimestamp(now)
        if period == "week":
            start = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


class MealStore:
    MAX_HISTORY = 400

    def __init__(self, data_dir: str, logger: Any) -> None:
        self._path = os.path.join(data_dir, "state.json")
        self._logger = logger
        self._lock = asyncio.Lock()
        self.data: dict = _blank()

    # ---------- 读写 ----------

    def load(self, default_enabled: bool = True) -> dict:
        try:
            with open(self._path, encoding="utf-8") as fp:
                raw = json.load(fp)
        except FileNotFoundError:
            self.data = _blank(default_enabled)
            self.save_sync()
            return self.data
        except (OSError, ValueError) as exc:
            self._logger.warning(f"饭票：state.json 读不出来（{exc}），按空状态重建")
            try:
                os.replace(self._path, self._path + ".corrupt")
            except OSError:
                pass
            self.data = _blank(default_enabled)
            self.save_sync()
            return self.data

        data = _blank(default_enabled)
        if isinstance(raw, dict):
            data.update(raw)
        if not isinstance(data.get("history"), list):
            data["history"] = []
        if not isinstance(data.get("meta"), dict):
            data["meta"] = {"last_ask_at": 0.0, "enabled": default_enabled, "day_key": "", "day_count": 0}
        data["meta"].setdefault("last_ask_at", 0.0)
        data["meta"].setdefault("enabled", default_enabled)
        data["meta"].setdefault("day_key", "")
        data["meta"].setdefault("day_count", 0)
        if not isinstance(data.get("pending"), dict):
            data["pending"] = None
        if not isinstance(data.get("current"), dict):
            data["current"] = None
        self.data = data
        return data

    def save_sync(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self.data, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, self._path)

    async def save(self) -> None:
        async with self._lock:
            try:
                await asyncio.to_thread(self.save_sync)
            except OSError as exc:
                self._logger.warning(f"饭票：state.json 写不进去：{exc}")

    # ---------- 开关与冷却 ----------

    @property
    def enabled(self) -> bool:
        return bool(self.data.get("meta", {}).get("enabled", True))

    def set_enabled(self, value: bool) -> None:
        self.data.setdefault("meta", {})["enabled"] = bool(value)

    @property
    def last_ask_at(self) -> float:
        return float(self.data.get("meta", {}).get("last_ask_at") or 0.0)

    def day_left(self, now: float, daily_max: int) -> int:
        """今天还能开口几次。跨天自动清零。"""
        key = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        meta = self.data.setdefault("meta", {})
        if meta.get("day_key") != key:
            meta["day_key"] = key
            meta["day_count"] = 0
        return max(0, int(daily_max) - int(meta.get("day_count") or 0))

    def bump_day(self) -> None:
        meta = self.data.setdefault("meta", {})
        meta["day_count"] = int(meta.get("day_count") or 0) + 1

    # ---------- 待回话的单 ----------

    def pending(self) -> dict | None:
        row = self.data.get("pending")
        return row if isinstance(row, dict) else None

    def pending_alive(self, now: float) -> dict | None:
        row = self.pending()
        if row and float(row.get("expires_at") or 0) > now:
            return row
        return None

    def expire_pending(self, now: float) -> dict | None:
        """过期没回的单落一条 expired 历史，只落一次。"""
        row = self.pending()
        if not row or float(row.get("expires_at") or 0) > now:
            return None
        self.data["pending"] = None
        self._push({**row, "status": "expired", "decided_at": float(now)})
        return row

    def open_pending(self, dish: Any, price: int, now: float,
                     ttl_min: int, attempt: int = 1, parent_id: str | None = None) -> dict:
        row = {
            "id": uuid4().hex[:12],
            "category": dish.category,
            "shop": dish.shop,
            "dish": dish.name,
            "price": int(price),
            "created_at": float(now),
            "expires_at": float(now + int(ttl_min) * 60),
            "attempt": int(attempt),
            "parent_id": parent_id,
        }
        self.data["pending"] = row
        self.data.setdefault("meta", {})["last_ask_at"] = float(now)
        return row

    def close_pending(self, status: str, now: float) -> dict | None:
        row = self.pending()
        if not row:
            return None
        self.data["pending"] = None
        done = {**row, "status": status, "decided_at": float(now)}
        if status != "approved":
            self._push(done)
        return done

    # ---------- 正在送 / 正在吃 ----------

    def current(self) -> dict | None:
        row = self.data.get("current")
        return row if isinstance(row, dict) else None

    def start_flight(self, order: dict, arrive_at: float, done_at: float) -> dict:
        """主人答应了，把这一单挂到 current 上往下推。"""
        row = {
            **order,
            "status": "approved",
            "decided_at": float(time.time()),
            "stage": "delivering",
            "arrive_at": float(arrive_at),
            "done_at": float(done_at),
        }
        self.data["current"] = row
        return row

    def advance(self, stage: str) -> dict | None:
        row = self.current()
        if not row:
            return None
        row["stage"] = stage
        return row

    def finish(self, now: float) -> dict | None:
        """吃完了，落历史。"""
        row = self.current()
        if not row:
            return None
        self.data["current"] = None
        self._push({**row, "stage": "done", "finished_at": float(now)})
        return row

    def _push(self, record: dict) -> None:
        rows = self.data.setdefault("history", [])
        rows.append(record)
        if len(rows) > self.MAX_HISTORY:
            del rows[: len(rows) - self.MAX_HISTORY]

    # ---------- 统计 ----------

    def summary(self, now: float, period: str = "month") -> dict:
        since = _period_start(now, period)
        rows = [o for o in (self.data.get("history") or [])
                if isinstance(o, dict) and float(o.get("decided_at") or 0) >= since]
        approved = [o for o in rows if o.get("status") == "approved"]
        rejected = [o for o in rows if o.get("status") == "rejected"]
        return {
            "count": len(rows),
            "approved": len(approved),
            "rejected": len(rejected),
            "spent": sum(int(o.get("price") or 0) for o in approved),
        }

    def recent(self, limit: int = 8) -> list[dict]:
        rows = [o for o in (self.data.get("history") or []) if isinstance(o, dict)]
        return list(reversed(rows))[: max(1, int(limit))]

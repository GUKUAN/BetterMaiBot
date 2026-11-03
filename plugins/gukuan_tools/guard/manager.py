"""at 归属守卫

挂在宿主原生的 reply 后处理钩子上（maisaka.reply.before_post_process），
允许改写正文和锚点。

今晚的实锤案例：一分钟内两个人先后 @ 她，planner 把 token 问题
挂到了 Yhcr 的消息上。软提示词管不住锚点选择，所以在出口处兜：
1) 正文 @ 的人和引用对象对不上 → 删 @ 保正文；
2) 引用对象和正文相关性低、而旁边另有别人 @ 她的消息相关性明显更高
  （字符二元组重合度，阈值保守）→ 把锚点改挂过去。
"""

import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

import asyncio


class AtGuardManager:
    """@ 归属守卫。数据库路径和 bot QQ 从配置来，不再硬编码。"""

    def __init__(self, core: Any) -> None:
        self.core = core
        self.name = "guard"

    async def setup(self) -> None:
        pass  # 无状态：每次请求现查宿主消息库

    async def teardown(self) -> None:
        pass

    def reload(self) -> None:
        pass

    @property
    def _db_path(self) -> str:
        return str(self.core.config.target.maibot_db)

    @property
    def _bot_uid(self) -> str:
        return str(self.core.config.target.bot_qq)

    async def handle(self, response: str, session_id: str, reply_message_id: str,
                     **kwargs: Any) -> dict[str, Any]:
        """钩子入口：返回带（可能改写过的）response 和 reply_message_id 的完整载荷。"""
        result: dict[str, Any] = {
            **kwargs,
            "response": response,
            "session_id": session_id,
            "reply_message_id": reply_message_id,
        }
        text = str(response or "")
        if not text or not reply_message_id:
            return result
        ats = re.findall(r"@\S+", text)
        if not ats:
            return result

        anchor = await asyncio.to_thread(self._query_message, reply_message_id)
        if not anchor:
            return result

        new_text = text
        anchor_names = self._names_of(anchor)
        stray = [a for a in ats if not self._name_matches(a, anchor_names)]
        if stray:
            for a in stray:
                new_text = new_text.replace(a, "")
            new_text = re.sub(r"\s{2,}", " ", new_text).strip()
            self.core.logger.info(f"at_guard：清掉与引用对象不符的 @ {stray}")

        try:
            better = await asyncio.to_thread(
                self._find_better_anchor, anchor, str(session_id), new_text
            )
            if better:
                result["reply_message_id"] = better["message_id"]
                self.core.logger.info(
                    f"at_guard：正文与原锚点相关性 {better['anchor_sim']:.2f}，"
                    f"改挂 {better['user_nickname']} 的消息（相似度 {better['sim']:.2f}）"
                )
        except Exception as exc:  # noqa: BLE001
            self.core.logger.debug(f"at_guard 锚点漂移检测失败: {exc}")

        result["response"] = new_text
        return result

    # ---------------------------------------------------------- 查库

    def _query_message(self, msg_id: str) -> dict | None:
        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
        except sqlite3.Error:
            return None
        try:
            row = conn.execute(
                "select message_id, user_id, user_nickname, processed_plain_text, timestamp "
                "from mai_messages where message_id = ?",
                (msg_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        if not row:
            return None
        return {
            "message_id": row[0], "user_id": row[1], "user_nickname": row[2],
            "text": row[3] or "", "ts": row[4],
        }

    @staticmethod
    def _names_of(anchor: dict) -> list:
        names = []
        for key in ("user_nickname", "user_id"):
            value = str(anchor.get(key) or "").strip()
            if value and value not in names:
                names.append(value)
        return names

    @staticmethod
    def _name_matches(at_token: str, names: list) -> bool:
        token = at_token.lstrip("@").lower()
        if not token:
            return False
        for name in names:
            low = (name or "").lower()
            if low and (low in token or token in low):
                return True
        return False

    @staticmethod
    def _sim(a: str, b: str) -> float:
        """中文字符二元组重合度，量小稳当，不引依赖。"""
        clean = lambda s: re.sub(r"[\s@，。？！,.?!:：;；()（）\[\]【】\"'「」]", "", s or "").lower()
        sa, sb = clean(a), clean(b)
        if len(sa) < 2 or len(sb) < 2:
            return 0.0
        ga = {sa[i:i + 2] for i in range(len(sa) - 1)}
        gb = {sb[i:i + 2] for i in range(len(sb) - 1)}
        inter = len(ga & gb)
        return inter / min(len(ga), len(gb)) if min(len(ga), len(gb)) else 0.0

    def _find_better_anchor(self, anchor: dict, session_id: str, text: str) -> dict | None:
        """找最近几分钟内别人 @ 她的消息里，和正文更相关的那条。"""
        anchor_sim = self._sim(text, anchor["text"])
        if anchor_sim >= 0.5:
            return None  # 锚点本身够相关，不动

        try:
            anchor_ts = datetime.fromisoformat(str(anchor["ts"]))
        except ValueError:
            return None
        since = (anchor_ts - timedelta(seconds=180)).isoformat()

        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
        except sqlite3.Error:
            return None
        try:
            rows = conn.execute(
                "select message_id, user_id, user_nickname, processed_plain_text "
                "from mai_messages where session_id = ? and timestamp >= ? "
                "and is_at = 1 and user_id != ? order by timestamp desc limit 20",
                (session_id, since, self._bot_uid),
            ).fetchall()
        except sqlite3.Error:
            return None
        finally:
            conn.close()

        best: dict | None = None
        for row in rows:
            if row[0] == anchor["message_id"]:
                continue
            sim = self._sim(text, row[3] or "")
            if sim > 0.3 and sim > anchor_sim + 0.15:
                if best is None or sim > best["sim"]:
                    best = {
                        "message_id": row[0], "user_nickname": row[2],
                        "sim": sim, "anchor_sim": anchor_sim,
                    }
        return best

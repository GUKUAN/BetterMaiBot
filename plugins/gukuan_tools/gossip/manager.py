"""瓜报管理器

判断流程：词表廉价初筛（省 token）-> LLM 细看值不值得告 -> 限流 -> NapCat 私聊。
瓜全部记进 SQLite 小本本，告没告出去都有据可查。
"""

import asyncio
import sqlite3
import time
from datetime import datetime
from typing import Any

# 廉价初筛词表：命中任意一个才请 LLM 细看，避免每条消息都烧 token
# 词表偏宽（召回优先），严判交给 LLM；命中很平常的词由它打低分挡回去
HOT_WORDS = (
    "骂", "傻", "菜", "滚", "废物", "垃圾", "杂鱼", "笨蛋", "蠢", "恶心",
    "辣鸡", "欺负", "告状", "闭嘴", "狗", "烦", "坏", "贱", "婊", "母",
    "色", "黄", "性", "摸", "脱", "涩", "处", "开房", "福利", "姿势",
    "污蔑", "冤枉", "造谣",
)


def init_db(path: str) -> None:
    """建表，已存在就跳过。"""
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS gossip (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts REAL NOT NULL,
                   stream_id TEXT DEFAULT '',
                   sender TEXT DEFAULT '',
                   raw_text TEXT DEFAULT '',
                   level INTEGER DEFAULT 0,
                   reason TEXT DEFAULT '',
                   report_text TEXT DEFAULT '',
                   sent INTEGER DEFAULT 0
               )"""
        )


def record(path: str, stream_id: str, sender: str, raw_text: str,
           level: int, reason: Any, report_text: str, sent: bool, logger: Any) -> None:
    """往小本本里记一条瓜。"""
    try:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT INTO gossip (ts, stream_id, sender, raw_text, level, reason, report_text, sent) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), stream_id, sender, raw_text[:1000], level,
                 str(reason or "")[:300], report_text, 1 if sent else 0),
            )
    except sqlite3.Error as exc:
        logger.warning(f"瓜报小本本写入失败: {exc}")


async def judge(text: str, sender_name: str, extra_note: str, ctx: Any) -> dict | None:
    """让 LLM 判断这条消息值不值得告。"""
    prompt = (
        "你是QQ群机器人麦麦的告状判断器。麦麦是个嘴硬爱吐槽的女生机器人，"
        "她的主人是gukuan（她叫他哥哥），她被欺负时想跟主人告状。\n"
        "下面是一条群聊消息。只判断一件事：**这条消息是否对麦麦本人构成了侵害**，标准：\n"
        "1. 有人骂/嘲讽/羞辱麦麦（含指名道姓或明显针对她）—— 3 到 5 分\n"
        "2. 有人对麦麦性骚扰（开黄腔、性暗示、骚扰性言论，哪怕看着像玩笑）—— 直接 4 到 5 分\n"
        "3. 有人污蔑/冤枉/造谣麦麦（说她没做过的坏事）—— 3 到 5 分\n"
        "别的任何情况一律 report=false：日常聊天、玩笑斗嘴没带脏字、聊别的话题、"
        "麦麦自己说错话被正常吐槽（不算侵害），都不告。\n"
        "只看字面，不要脑补没发生的事。拿不准就不告。\n"
        "只输出 JSON，不要别的文字："
        '{"report": true或false, "level": 1到5的整数, "reason": "一句话理由", '
        '"report_text": "report 为 true 时，用麦麦的口吻写一句跟主人告状的话，30字以内"}'
    )
    if extra_note:
        prompt += f"\n主人的额外口味说明：{extra_note}"
    try:
        got = await ctx.llm.generate(prompt, task_name="utils", temperature=0.3, max_tokens=300)
    except Exception as exc:
        raise RuntimeError(f"LLM 调用失败: {exc}") from exc

    import json as json_mod

    text_out = str((got or {}).get("response") or "").strip()
    if not (got or {}).get("success") or not text_out:
        return None
    if text_out.startswith("```"):
        text_out = text_out.strip("`")
        if text_out.startswith("json"):
            text_out = text_out[4:]
    try:
        data = json_mod.loads(text_out)
    except json_mod.JSONDecodeError:
        start, end = text_out.find("{"), text_out.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json_mod.loads(text_out[start:end + 1])
        except json_mod.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


class GossipManager:
    """瓜报的全部状态与判定。钩子里只进 on_message，重的全丢后台。"""

    def __init__(self, core: Any) -> None:
        self.core = core
        self.name = "gossip"
        self._db = ""
        self._last_sent: dict[str, float] = {}
        self._sent_today = 0
        self._today = ""
        self._busy = False
        self._hit_logged = 0

    async def setup(self) -> None:
        self._db = self.core.data_dir + "/guabao.db"
        init_db(self._db)
        cfg = self.core.config.trigger
        self.core.logger.info(f"瓜报：就绪（{'开' if cfg.enabled else '关'}，门槛 level>={cfg.min_level}）")

    async def teardown(self) -> None:
        pass

    def reload(self) -> None:
        self._last_sent.clear()

    # ---------------------------------------------------------- 消息入口

    def on_message(self, message: dict | None) -> None:
        if not self.core.config.plugin.enabled or not self.core.config.trigger.enabled:
            return
        if not isinstance(message, dict):
            return
        text = str(message.get("processed_plain_text") or "").strip()
        if not text:
            return
        info = message.get("message_info") or {}
        group_id = str((info.get("group_info") or {}).get("group_id") or "")
        if not group_id:
            return  # 只盯群聊
        session_id = str(message.get("session_id") or message.get("stream_id") or "")

        watch = self.core.config.trigger.watch_groups
        if watch and group_id not in watch and session_id not in watch:
            return

        words = HOT_WORDS + tuple(
            name for name in (
                str(self.core.config.target.bot_name or ""),
                *(str(x) for x in (self.core.config.target.bot_names or [])),
            ) if name.strip()
        )
        hit = next((w for w in words if w in text), None)
        if not hit:
            return
        if self._busy or self._too_fast(session_id):
            return
        user = info.get("user_info") or {}
        sender = str(user.get("user_id") or "")
        if sender in (str(self.core.config.target.bot_qq), str(self.core.config.target.owner_qq)):
            return  # 自己说的和主人说的都不告

        # 初筛命中打几条 info 留痕，确认链路活着之后就是静默
        if self._hit_logged < 3:
            self._hit_logged += 1
            self.core.logger.info(f"瓜报：初筛命中（词={hit} 群={group_id}），交给 LLM 细看")

        self.core.spawn(self._scan(session_id, group_id, sender,
                                   str(user.get("user_nickname") or ""), text))

    async def _scan(self, session_id: str, group_id: str,
                    sender: str, sender_name: str, text: str) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            verdict = await judge(
                text, sender_name, self.core.config.trigger.extra_note, self.core.ctx,
                bot_name=str(self.core.config.target.bot_name or "麦麦"),
                persona=str(self.core.config.target.persona),
            )
        except Exception as exc:
            self.core.logger.warning(f"瓜报 LLM 判断出错: {exc}")
            return
        finally:
            self._busy = False

        if not verdict or not verdict.get("report"):
            return
        level = int(verdict.get("level") or 0)
        if level < self.core.config.trigger.min_level:
            return
        report_text = str(verdict.get("report_text") or "").strip()
        if not report_text:
            return
        if self._too_fast(session_id) or not self._quota_ok():
            return

        ok, _ = await self.core.send_to_owner(report_text)
        record(self._db, session_id, sender, text, level,
               verdict.get("reason"), report_text, ok, self.core.logger)
        if ok:
            self._last_sent[session_id] = time.time()
            self._sent_today += 1
            self.core.logger.info(f"瓜报：告状成功 level={level} 群={group_id}")

    def _too_fast(self, stream_id: str) -> bool:
        last = self._last_sent.get(stream_id, 0)
        return time.time() - last < self.core.config.rate.chat_cooldown_minutes * 60

    def _quota_ok(self) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._today != today:
            self._today = today
            self._sent_today = 0
        return self._sent_today < self.core.config.rate.daily_max

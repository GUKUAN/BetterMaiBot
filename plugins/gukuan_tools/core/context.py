"""各功能公用的运行环境

功能模块（guard/gossip/meal/geng）都不直接碰 SDK 的 plugin 实例，
想要发消息、补历史、起后台任务，都从这里拿 —— 换句话说，
一个功能只要拿着 PluginContext 就能完整运转，测试和搬迁都不用拖着整个插件走。
"""

import asyncio
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

PLUGIN_ID = "gukuan.tools"


def sid_of(kwargs: dict) -> str:
    """从命令参数里挖出会话 ID。

    宿主是拿 message=SessionMessage 传进来的，但过一道 RPC 之后到插件这边
    可能已经是 dict 了，两种都得认。实在没有就再往 kwargs 顶层找。
    """
    msg = kwargs.get("message")
    if isinstance(msg, dict):
        for key in ("session_id", "stream_id"):
            value = msg.get(key)
            if value:
                return str(value)
        inner = msg.get("message_info")
        if isinstance(inner, dict) and inner.get("session_id"):
            return str(inner["session_id"])
    elif msg is not None:
        for key in ("session_id", "stream_id"):
            value = getattr(msg, key, None)
            if value:
                return str(value)

    for key in ("stream_id", "session_id"):
        value = kwargs.get(key)
        if value:
            return str(value)
    return ""


class PluginContext:
    """功能的公共依赖。反作弊那边对应 PluginLoader 的位置：功能要什么，这里给什么。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._jobs: set[asyncio.Task] = set()
        self._stream = ""  # 主人私聊的会话 ID，懒加载

    # ---------- 基础设施 ----------

    @property
    def ctx(self) -> Any:
        return self._plugin.ctx

    @property
    def config(self) -> Any:
        return self._plugin.config

    @property
    def logger(self) -> Any:
        return self.ctx.logger

    @property
    def data_dir(self) -> str:
        base = str(self.ctx.paths.data_dir)
        import os

        os.makedirs(base, exist_ok=True)
        return base

    @property
    def owner_stream_id(self) -> str:
        """主人私聊会话 ID（可能为空，等 stream_id() 懒加载后才有）。"""
        return self._stream

    def spawn(self, coro: Any) -> None:
        """丢后台并持有引用，否则任务可能被 GC 静默吞掉。"""
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()  # 已经没有 loop 了（卸载中），直接丢弃
            return
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    async def later(self, delay: float, fn: Any, *args: Any) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
        except asyncio.CancelledError:
            raise
        await fn(*args)

    async def teardown_jobs(self) -> None:
        """卸载时清一次性任务：不清的话 reload 后那些 sleep 到半小时后的任务会幽灵发言。"""
        rest = [t for t in self._jobs if not t.done()]
        for job in rest:
            job.cancel()
        if rest:
            await asyncio.gather(*rest, return_exceptions=True)
        self._jobs.clear()

    # ---------- 说话对象 ----------

    def life_skip(self, now: float) -> str:
        """参考 life-frequency 的状态，返回不该开口的理由。空串表示可以开口。

        读不到就当它不存在（fail-open）——别人的插件改了文件格式，
        不该把我们的饭点一起搞死。
        """
        cfg = self.config.life
        if not cfg.enabled:
            return ""
        try:
            with open(cfg.state_path, encoding="utf-8") as fp:
                state = json.load(fp)
        except (OSError, ValueError):
            return ""
        if not isinstance(state, dict):
            return ""
        activity = str(state.get("activity") or "")
        if activity in ("sleep", "nap"):
            return f"她在{activity}"
        satiety = float(state.get("satiety") or 0)
        if satiety > cfg.max_satiety:
            return f"饱腹 {satiety:.1f}"
        last_meal = float(state.get("last_meal_at") or 0)
        if last_meal > 0 and now - last_meal < cfg.min_meal_gap_min * 60:
            return "刚吃过没多久"
        return ""

    # ---------- 发送 ----------

    async def send_to_owner(self, text: str) -> tuple[bool, str]:
        """发一条私信给主人。返回 (成功, 用的 stream_id)。

        链路：宿主 send.text（带历史同步）-> 清缓存重解析一次 -> NapCat 兜底。
        """
        sid = await self.stream_id()
        if sid and await self._push_host(text, sid):
            return True, sid
        if sid:
            # 缓存的 stream 可能已经失效（宿主重启/会话重建），强刷一次再试
            self._stream = ""
            sid = await self.stream_id(force=True)
            if sid and await self._push_host(text, sid):
                return True, sid
        self.logger.warning("饭票：宿主发不出去，降级 NapCat")
        return await asyncio.to_thread(self._push_napcat, text), ""

    async def stream_id(self, force: bool = False) -> str:
        """拿主人私聊的 stream_id。

        open_session 会顺手把会话建出来，返回顶层就有 stream_id；
        建不出来再退回 get_stream_by_user_id 反查已存在的会话。
        """
        if self._stream and not force:
            return self._stream
        owner = str(self.config.target.owner_qq)
        sid = ""
        try:
            got = await self.ctx.chat.open_session(platform="qq", chat_type="private", user_id=owner)
        except Exception as exc:
            self.logger.warning(f"饭票：open_session 出错：{exc}")
            got = {}
        if isinstance(got, dict):
            sid = str(got.get("stream_id") or "")
            if not sid and isinstance(got.get("stream"), dict):
                sid = str(got["stream"].get("stream_id") or "")
        if not sid:
            try:
                info = await self.ctx.chat.get_stream_by_user_id(user_id=owner)
            except Exception as exc:
                self.logger.warning(f"饭票：get_stream_by_user_id 出错：{exc}")
                info = {}
            if isinstance(info, dict):
                stream = info.get("stream") if isinstance(info.get("stream"), dict) else info
                sid = str((stream or {}).get("stream_id") or "")
        self._stream = sid
        return sid

    async def _push_host(self, text: str, sid: str) -> bool:
        try:
            got = await self.ctx.send.text(
                text, sid,
                sync_to_maisaka_history=True,
                maisaka_source_kind=f"plugin:{PLUGIN_ID}",
            )
        except TypeError:
            # 老版本 SDK 不认这两个参数，去掉再发一次
            try:
                got = await self.ctx.send.text(text, sid)
            except Exception as exc:
                self.logger.warning(f"饭票：send.text 出错：{exc}")
                return False
        except Exception as exc:
            self.logger.warning(f"饭票：send.text 出错：{exc}")
            return False
        if isinstance(got, dict):
            return bool(got.get("success"))
        return bool(got)

    def _push_napcat(self, text: str) -> bool:
        cfg = self.config.target
        payload = json.dumps({
            "user_id": int(cfg.owner_qq),
            "message": [{"type": "text", "data": {"text": text}}],
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if cfg.napcat_token:
            headers["Authorization"] = f"Bearer {cfg.napcat_token}"
        req = urllib.request.Request(
            f"{cfg.napcat_http_url.rstrip('/')}/send_private_msg",
            data=payload, headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
                return body.get("retcode") == 0
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
            self.logger.warning(f"饭票：NapCat 兜底也失败了：{exc}")
            return False

    async def note(self, fact: str, sid: str = "") -> None:
        """把「她为什么说了这句话」补进上下文。

        注意段里的文本键必须是 data —— 宿主只认 data/content，
        写成 text 不报错，入库存的是空串。
        """
        sid = sid or self._stream
        if not sid:
            return
        try:
            got = await self.ctx.maisaka.context.append(
                sid,
                [{"type": "text", "data": fact}],
                visible_text=fact,
                source_kind=f"plugin:{PLUGIN_ID}",
            )
        except Exception as exc:
            self.logger.debug(f"饭票：补历史出错：{exc}")
            return
        if isinstance(got, dict) and got.get("success") is False:
            self.logger.debug(f"饭票：补历史被宿主拒绝：{got.get('error')}")

    async def reply_command(self, kwargs: dict, text: str) -> bool:
        """命令自己回话。

        宿主那套命令链拿到 (success, response, intercept_level) 之后
        只把 response 打进日志，不代发。所以想让她开口就只能自己调 send。
        """
        sid = sid_of(kwargs)
        if not sid:
            self.logger.warning(
                f"命令回复失败：拿不到会话 ID，kwargs 键={sorted(kwargs.keys())}（{text[:20]}）"
            )
            return False
        try:
            got = await self.ctx.send.text(text, sid)
        except Exception as exc:
            self.logger.warning(f"命令回复出错: {exc}")
            return False
        if isinstance(got, dict):
            return bool(got.get("sent"))
        return bool(got)

    # ---------- 话术 ----------

    async def phrase(self, kind: str, dish: Any, price: int, prompts: dict, fallbacks: dict) -> str:
        """生成一句话。LLM 挂了就用写死的台词，绝不因此把这一单扔掉。"""
        fallback = fallbacks[kind].format(dish=dish.name, price=price)
        prompt = prompts[kind].format(
            dish=dish.name, shop=dish.shop, price=price,
            bot_name=str(self.config.target.bot_name or "麦麦"),
        )
        try:
            got = await self.ctx.llm.generate(prompt, task_name="utils", temperature=0.85, max_tokens=120)
        except Exception as exc:
            self.logger.warning(f"饭票：LLM 生成出错，用兜底台词：{exc}")
            return fallback

        if not isinstance(got, dict) or not got.get("success"):
            self.logger.info("饭票：LLM 没生成出来，用兜底台词")
            return fallback

        text = str(got.get("response") or "").strip()
        text = text.splitlines()[0].strip() if text else ""
        text = text.strip("\"'“”「」『』` \t")
        if not text or len(text) > 60:
            return fallback
        return text

    def now(self) -> datetime:
        return datetime.now()

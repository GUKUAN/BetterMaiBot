"""麦麦工具箱 —— 自研杂项（v3，manager 化）

四个功能，各自一个 Manager，在 on_load 里注册进 FeatureRegistry：
  guard   归属守卫  回复正文 @ 的人和引用对象不符时兜底修正
  gossip  瓜报      群里受了委屈就私聊主人打小报告
  meal    饭票      饭点跟主人撒娇要外卖，答应就演「到啦」「吃完了」
  geng    新梗      每天捞新梗写进黑话库，碰上顺口的主动用一次

这个文件只做两件事：把 SDK 的 hook/command/tool 入口挂住，然后一行转发给
对应的 Manager。所有业务逻辑都在各功能的模块里 —— 想看某个功能怎么运转，
直接翻它的目录，不用在入口文件里刨。
"""

from typing import Any

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

try:
    from .config_sections import ToolsConfig
    from .core import PluginContext, FeatureRegistry
    from .geng.manager import GengManager
    from .guard.manager import AtGuardManager
    from .gossip.manager import GossipManager
    from .meal.manager import MealManager
except ImportError:  # 平铺加载兜底，方便单独跑脚本验证
    from config_sections import ToolsConfig
    from core import PluginContext, FeatureRegistry
    from geng.manager import GengManager
    from guard.manager import AtGuardManager
    from gossip.manager import GossipManager
    from meal.manager import MealManager

PLUGIN_ID = "gukuan.tools"


class GukuanTools(MaiBotPlugin):
    """麦麦工具箱：guard + gossip + meal + geng"""

    config_model = ToolsConfig

    def __init__(self) -> None:
        super().__init__()
        self.core = PluginContext(self)
        self.registry = FeatureRegistry()
        self.registry.register(AtGuardManager(self.core))
        self.registry.register(GossipManager(self.core))
        self.registry.register(MealManager(self.core))
        self.registry.register(GengManager(self.core))

    # SDK 这边有些地方要求直接访问插件实例的属性，转发出去
    @property
    def meal(self) -> MealManager:
        return self.registry.get("meal")

    # ---------------------------------------------------------- 生命周期

    async def on_load(self) -> None:
        await self.registry.setup_all()
        self.ctx.logger.info("麦麦工具箱已加载：" + "；".join(
            f"{feature.name} ✓" for feature in self.registry.all
        ))

    async def on_unload(self) -> None:
        await self.registry.teardown_all()
        self.ctx.logger.info("麦麦工具箱已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        self.ctx.logger.info(f"工具箱配置已更新 scope={scope} version={version}")
        self.registry.reload_all()

    # ---------------------------------------------------------- 消息旁路

    @HookHandler(
        "chat.receive.after_process",
        name="tools_watch",
        description="只读旁路：找值得告状的瓜 + 接主人的买/不买 + 听主人喊饿",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=1500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def watch(self, message: dict | None = None, **kwargs: Any) -> dict[str, Any]:
        """钩子里只做零成本的判定，重的全丢后台。两件事分开 try，一个挂了不连累另一个。"""
        del kwargs
        gossip, meal = self.registry.get("gossip"), self.registry.get("meal")
        try:
            gossip.on_message(message)
        except Exception as exc:
            self.ctx.logger.debug(f"瓜报快扫出错: {exc}")
        try:
            meal.on_message(message)
        except Exception as exc:
            self.ctx.logger.debug(f"饭票判定出错: {exc}")
        return {"action": "continue"}

    # ---------------------------------------------------------- at 归属守卫

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="at_belong_guard",
        description="回复正文 @ 的人与被引用消息的发送者不符时清掉；正文明显在答别人的 @ 时改挂锚点",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=5000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def at_guard(
        self,
        response: str = "",
        session_id: str = "",
        reply_message_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.registry.get("guard").handle(
            response=response, session_id=session_id, reply_message_id=reply_message_id, **kwargs
        )

    # ---------------------------------------------------------- 新梗的三个钩子

    @HookHandler(
        "jargon.extract.before_persist",
        name="geng_watch",
        description="旁路看一眼原生学到了哪些词，没释义的记下来等联网补课",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=800,
        error_policy=ErrorPolicy.SKIP,
    )
    async def watch_geng(self, entries: list | None = None, **kwargs: Any) -> dict[str, Any]:
        return await self.registry.get("geng").on_jargon_entries(entries)

    @HookHandler(
        "maisaka.planner.before_request",
        name="geng_play",
        description="有合适的梗就递一句，用不用让她自己判断",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=1200,
        error_policy=ErrorPolicy.SKIP,
    )
    async def play_geng(self, **kwargs: Any) -> dict[str, Any]:
        return await self.registry.get("geng").play_hook(**kwargs)

    # ---------------------------------------------------------- 饭票注入

    @HookHandler(
        "maisaka.planner.before_request",
        name="meal_inject",
        description="每轮把点单状态折进 Planner 的首个 system 项",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=2500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject(self, **kwargs: Any) -> dict[str, Any]:
        return await self.meal.inject_hook(**kwargs)

    # ---------------------------------------------------------- 查梗工具

    @Tool(
        "cha_geng",
        brief_description="查网络梗 / 网络流行语是什么意思",
        detailed_description=(
            "遇到看不懂的网络用语、梗、缩写、谐音梗时调用，比联网搜索更对口。\n"
            "会依次查萌娘百科和联网搜索，返回一句人话解释。\n"
            "返回值是给你参考的意思，请用你自己平时的口吻自然回答，不要照搬原文、不要列编号。\n"
            "参数：\n"
            "- term：string，必填。要查的那个词本身，不要整句话。"
        ),
        parameters=[
            ToolParameterInfo(
                name="term",
                param_type=ToolParamType.STRING,
                description="要查的梗或网络用语，比如「栓Q」「蚌埠住了」",
                required=True,
            ),
        ],
    )
    async def handle_cha_geng(self, term: str = "", **kwargs: Any):
        del kwargs
        word = str(term or "").strip()
        if not word:
            return {"success": False, "content": "（没给要查的词）"}
        return await self.registry.get("geng").tool_lookup(word)

    # ---------------------------------------------------------- 饭票命令

    @Command("投喂", description="立刻起一单（测试用，跳过冷却和概率）", pattern=r"^/投喂$", permission="operator")
    async def cmd_feed(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        await self.meal.cmd_feed(kwargs)
        return True, None, 1

    @Command("账本", description="查虚拟账本", pattern=r"^/账本(?:\s+(?P<period>本周|上周|本月|上月))?$", permission="operator")
    async def cmd_ledger(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        period = str((kwargs.get("matched_groups") or {}).get("period") or "本月")
        await self.meal.cmd_ledger(kwargs, period)
        return True, None, 1

    @Command("饭史", description="看最近几单", pattern=r"^/饭史(?:\s+(?P<count>\d{1,2}))?$", permission="operator")
    async def cmd_history(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        try:
            count = int((kwargs.get("matched_groups") or {}).get("count") or 8)
        except (TypeError, ValueError):
            count = 8
        await self.meal.cmd_history(kwargs, count)
        return True, None, 1

    @Command("饭开关", description="开关主动喊饿", pattern=r"^/饭开关(?:\s+(?P<action>开|关|on|off))?$", permission="operator")
    async def cmd_toggle(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        action = str((kwargs.get("matched_groups") or {}).get("action") or "").strip().lower()
        await self.meal.cmd_toggle(kwargs, action)
        return True, None, 1

    # ---------------------------------------------------------- 梗命令

    @Command("梗", description="看梗库统计", pattern=r"^/梗$", permission="operator")
    async def cmd_geng(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        await self.registry.get("geng").cmd_stats(kwargs)
        return True, None, 1

    @Command("梗跑", description="立刻跑一轮采集", pattern=r"^/梗跑$", permission="operator")
    async def cmd_geng_run(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        await self.registry.get("geng").cmd_run(kwargs)
        return True, None, 1

    @Command("梗冷启动", description="从标题池里回填旧梗", pattern=r"^/梗冷启动(?:\s+(?P<count>\d{1,4}))?$", permission="operator")
    async def cmd_geng_cold(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        try:
            want = int((kwargs.get("matched_groups") or {}).get("count") or 40)
        except (TypeError, ValueError):
            want = 40
        await self.registry.get("geng").cmd_cold(kwargs, want)
        return True, None, 1

    @Command("梗找", description="现查一个梗", pattern=r"^/梗找\s+(?P<term>\S{1,20})$", permission="operator")
    async def cmd_geng_lookup(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        term = str((kwargs.get("matched_groups") or {}).get("term") or "").strip()
        await self.registry.get("geng").cmd_lookup(kwargs, term)
        return True, None, 1

    @Command("梗审", description="看待审队列", pattern=r"^/梗审$", permission="operator")
    async def cmd_geng_pending(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        await self.registry.get("geng").cmd_pending(kwargs)
        return True, None, 1

    @Command("梗收", description="收下一条待审", pattern=r"^/梗收\s+(?P<pid>\d{1,6})$", permission="operator")
    async def cmd_geng_take(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        try:
            pid = int((kwargs.get("matched_groups") or {}).get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        await self.registry.get("geng").cmd_take(kwargs, pid)
        return True, None, 1

    @Command("梗弃", description="丢掉一条待审", pattern=r"^/梗弃\s+(?P<pid>\d{1,6})$", permission="operator")
    async def cmd_geng_drop(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        try:
            pid = int((kwargs.get("matched_groups") or {}).get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        await self.registry.get("geng").cmd_drop(kwargs, pid)
        return True, None, 1

    @Command("梗玩", description="让她下一轮试着玩个梗", pattern=r"^/梗玩(?:\s+(?P<term>\S{1,20}))?$", permission="operator")
    async def cmd_geng_play_cmd(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        term = str((kwargs.get("matched_groups") or {}).get("term") or "").strip()
        await self.registry.get("geng").cmd_play(kwargs, term)
        return True, None, 1

    @Command("梗开关", description="开关新梗模块（只改本次运行）", pattern=r"^/梗开关(?:\s+(?P<action>开|关|on|off))?$", permission="operator")
    async def cmd_geng_toggle(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        action = str((kwargs.get("matched_groups") or {}).get("action") or "").strip().lower()
        await self.registry.get("geng").cmd_toggle(kwargs, action)
        return True, None, 1


def create_plugin() -> MaiBotPlugin:
    """SDK 约定的入口"""
    return GukuanTools()

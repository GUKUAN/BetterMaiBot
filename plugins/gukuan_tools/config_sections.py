"""配置段定义

按功能域分组：基础/说话对象是公共的，瓜报、饭票、新梗各自一段。
字段和旧版完全一致，线上 config.toml 可以无缝沿用。
"""

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """总开关"""

    __ui_label__ = "基础设置"

    config_version: str = Field(default="3.0.0", description="配置版本号")
    enabled: bool = Field(default=True, description="总开关，关掉后告状和饭票都不跑")


class TargetSection(PluginConfigBase):
    """跟谁说话、走哪条网"""

    __ui_label__ = "说话对象"

    owner_qq: int = Field(default=10000, description="主人 QQ，告状和点单都发给他")
    bot_qq: int = Field(default=10001, description="麦麦自己的 QQ")
    bot_name: str = Field(default="麦麦", description="她在话术和告状判断里被怎么称呼")
    bot_names: list[str] = Field(
        default_factory=lambda: ["麦麦", "mai"],
        description="她的名字/昵称，瓜报词表会动态补上这些，用来识别骂到她头上的消息",
    )
    persona: str = Field(
        default="嘴硬爱吐槽的女生机器人",
        description="一句话人设，告状判断的 prompt 会用到；改名换人设只改这里",
    )
    owner_names: list[str] = Field(
        default_factory=lambda: ["哥哥", "主人"],
        description="主人可能被叫的称呼，用于 LLM 识别",
    )
    napcat_http_url: str = Field(default="http://napcat:3900", description="NapCat HTTP 地址，兜底发送用")
    napcat_token: str = Field(default="", description="NapCat 鉴权 token，无则留空")
    maibot_db: str = Field(
        default="/MaiMBot/data/MaiBot.db",
        description="宿主消息库路径（容器内绝对路径），归属守卫查引用用",
    )


class TriggerSection(PluginConfigBase):
    """瓜报·触发"""

    __ui_label__ = "瓜报·触发"

    enabled: bool = Field(default=True, description="是否启用告状")
    min_level: int = Field(default=3, description="告状门槛，LLM 打分 1-5，达到才告", ge=1, le=5)
    watch_groups: list[str] = Field(
        default_factory=list,
        description="只盯这些群（群号或 session_id），留空表示所有群都盯",
    )
    extra_note: str = Field(default="", description="追加给 LLM 的告状口味说明")


class RateSection(PluginConfigBase):
    """瓜报·限流"""

    __ui_label__ = "瓜报·限流"

    chat_cooldown_minutes: int = Field(default=45, description="同一个群两次告状的最小间隔（分钟）", ge=1)
    daily_max: int = Field(default=8, description="每天最多告状条数（全局）", ge=1)
    llm_timeout_seconds: int = Field(default=60, description="LLM 判断超时（秒）", ge=5)


class MealSection(PluginConfigBase):
    """饭票·时机"""

    __ui_label__ = "饭票·时机"

    enabled: bool = Field(default=True, description="是否启用饭票")
    meal_times: list[str] = Field(
        default_factory=lambda: ["11:10", "17:10", "20:40"],
        description="饭点列表（24 小时制 HH:MM）。默认卡在生活插件的午餐 12:00、晚餐 18:00 之前",
    )
    probability: float = Field(default=0.7, ge=0.0, le=1.0, description="每个饭点真的开口的概率")
    delay_max_min: int = Field(default=8, ge=0, description="开口前随机延迟上限（分钟），0 表示准点")
    cooldown_min: int = Field(default=240, ge=0, description="两次起单的最小间隔（分钟）")
    pending_ttl_min: int = Field(default=30, ge=1, description="一单等主人回话的有效期（分钟）")
    chance_in_chat: float = Field(default=0.35, ge=0.0, le=1.0, description="主人随口喊饿时真的起单的概率")
    daily_max: int = Field(default=3, ge=1, description="每天最多主动开口几次")
    arrive_min: int = Field(default=30, ge=1, description="主人答应之后多久「到啦」（分钟）")
    eat_min: int = Field(default=18, ge=1, description="「到啦」之后多久「吃完了」（分钟）")
    allow_group: bool = Field(default=False, description="是否在群里也认主人的「买吧」（默认只在私聊）")
    retry_once: bool = Field(default=True, description="被拒后换个更便宜的再问一次")
    retry_delay_min: int = Field(default=5, ge=1, description="二次开口的最短等待（分钟）")
    retry_delay_max_min: int = Field(default=15, ge=1, description="二次开口的最长等待（分钟）")
    retry_price_ratio: float = Field(default=0.6, gt=0.0, le=1.0, description="二次开口的价格上限 = 上次 × 该比例")
    active_enabled: bool = Field(default=True, description="首次启动时主动开口的默认状态")
    emotion_window: int = Field(default=20, ge=4, le=200, description="用最近多少单的答应率决定她的语气档位")


class MenuSection(PluginConfigBase):
    """饭票·菜单池"""

    __ui_label__ = "饭票·菜单"

    pool: list[str] = Field(
        default_factory=lambda: [
            "快餐|沙县小吃|拌面配蒸饺|12|18",
            "快餐|兰州拉面|牛肉面|15|22",
            "快餐|黄焖鸡|黄焖鸡米饭|18|26",
            "麻辣|杨国福|麻辣烫|22|38",
            "炸鸡|肯德基|吮指原味鸡套餐|32|45",
            "炸鸡|华莱士|全家桶|35|55",
            "日料|吉野家|牛丼饭|28|36",
            "日料|寿司郎|寿司拼盘|60|95",
            "火锅|海底捞|单人小火锅|58|88",
            "烧烤|木屋烧烤|烤串拼盘|45|70",
            "甜品|蜜雪冰城|奶茶配冰淇淋|10|16",
            "甜品|喜茶|多肉葡萄|18|25",
            "夜宵|楼下小摊|炒粉加烤肠|12|20",
            "夜宵|大排档|蒜香小龙虾|68|120",
        ],
        description='一行一个菜，格式「分类|店名|菜名|最低价|最高价」，例如 "麻辣|杨国福|麻辣烫|22|38"',
    )


class MoneySection(PluginConfigBase):
    """饭票·记账"""

    __ui_label__ = "饭票·记账"

    currency: str = Field(default="¥", description="金额前缀符号")
    month_budget: int = Field(default=800, ge=0, description="每月虚拟预算，只用于 /账本 提示，0 表示不限")


class LifeSection(PluginConfigBase):
    """跟 life-frequency 打交道：她已经吃饱了就别再喊饿"""

    __ui_label__ = "生活插件兼容"

    enabled: bool = Field(default=True, description="是否读 life-frequency 的状态再做判断")
    state_path: str = Field(
        default="/MaiMBot/data/plugins/org.orge-8.life-frequency/life_state.json",
        description="life-frequency 的状态文件路径（容器内绝对路径）",
    )
    max_satiety: float = Field(default=7.0, description="饱腹超过这个值就不开口")
    min_meal_gap_min: int = Field(default=180, description="距上次吃饭不足这么多分钟就不开口")


class GengSection(PluginConfigBase):
    """新梗·总控"""

    __ui_label__ = "新梗·总控"

    enabled: bool = Field(default=True, description="关掉后不再采集，也不主动抛梗")
    run_at: str = Field(default="04:30", description="每天几点跑一轮（HH:MM），卡在备份脚本 04:10 之后")
    push_threshold: float = Field(default=0.75, ge=0.0, le=1.0, description="置信度到多少才直接进全局黑话库")
    pending_floor: float = Field(default=0.40, ge=0.0, le=1.0, description="低于这个值的直接丢，中间档存待审")
    round_cap: int = Field(default=5, ge=1, description="一轮最多进库几条")
    lib_cap: int = Field(default=150, ge=0, description="本插件贡献的全局梗上限，满了只补不增")
    llm_task: str = Field(default="utils", description="调模型用的 task_name")


class GengMoegirlSection(PluginConfigBase):
    """新梗·萌娘百科"""

    __ui_label__ = "新梗·萌百"

    enabled: bool = Field(default=True, description="从萌百 sitemap 抓新增条目")
    sitemap_index: str = Field(
        default="https://zh.moegirl.org.cn/sitemap/sitemap-index-zhmoegirl.xml",
        description="robots.txt 里声明的 sitemap 索引地址",
    )
    shards: int = Field(default=4, ge=1, le=8, description="取几个 NS_0 分片")
    ua: str = Field(
        default="maibot-geng/1.0 (self-use bot)",
        description="请求 UA。萌百对匿名 UA 不挑，但写清楚用途更礼貌",
    )
    timeout: int = Field(default=20, ge=5, description="单次请求超时（秒）")
    batch: int = Field(default=40, ge=5, le=80, description="一批丢多少个标题给模型判定")
    prefer_html: bool = Field(
        default=False,
        description="只走 /wiki/ 干净 URL，完全不碰 api.php（robots 更严格时用）",
    )


class GengSearxSection(PluginConfigBase):
    """新梗·搜索那一路"""

    __ui_label__ = "新梗·搜索"

    enabled: bool = Field(default=True, description="用 searxng 搜最近的热梗补漏")
    base_url: str = Field(
        default="http://searxng:8080",
        description="容器网络里的 searxng 地址，别写宿主侧的 127.0.0.1:8800",
    )
    queries: list[str] = Field(
        default_factory=lambda: [
            "最近网络热梗", "{ym} 网络流行语", "B站 新梗 是什么意思", "梗百科 最新",
        ],
        description="搜索词模板，{ym} 会替换成「2026年10月」这种",
    )
    per_query: int = Field(default=8, ge=1, le=20, description="每条 query 取几个结果")


class GengPlaySection(PluginConfigBase):
    """新梗·主动玩梗"""

    __ui_label__ = "新梗·玩梗"

    enabled: bool = Field(default=True, description="学到好玩的梗会不会主动抛一次")
    min_fun: int = Field(default=4, ge=1, le=5, description="好玩度到几才排进待玩队列")
    chance: float = Field(default=0.35, ge=0.0, le=1.0, description="每轮对话真的开口的概率")
    daily_max: int = Field(default=3, ge=1, description="每天最多主动抛几次")
    cooldown_min: int = Field(default=90, ge=1, description="两次抛梗的最小间隔（分钟）")
    max_times: int = Field(default=2, ge=1, description="同一个梗最多抛几次就收手")
    max_age_days: int = Field(
        default=30, ge=0,
        description="只主动玩这么多天内采到的梗，0 表示不限。老梗交给原生黑话系统（被提到时才懂）",
    )
    watch_groups: list[str] = Field(
        default_factory=list,
        description="只在哪些群玩（群号或 session_id），留空表示所有群",
    )
    allow_private: bool = Field(default=True, description="私聊里也玩")


class GengPushSection(PluginConfigBase):
    """新梗·写库"""

    __ui_label__ = "新梗·写库"

    webui: str = Field(default="http://127.0.0.1:8001", description="容器内 WebUI 地址")
    webui_json: str = Field(
        default="/MaiMBot/data/webui.json",
        description="WebUI 配置文件，里面存着 access_token",
    )
    token: str = Field(default="", description="留空则从 webui.json 读")


class ToolsConfig(PluginConfigBase):
    """工具箱配置"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    target: TargetSection = Field(default_factory=TargetSection)
    trigger: TriggerSection = Field(default_factory=TriggerSection)
    rate: RateSection = Field(default_factory=RateSection)
    meal: MealSection = Field(default_factory=MealSection)
    menu: MenuSection = Field(default_factory=MenuSection)
    money: MoneySection = Field(default_factory=MoneySection)
    life: LifeSection = Field(default_factory=LifeSection)
    geng: GengSection = Field(default_factory=GengSection)
    gengmoegirl: GengMoegirlSection = Field(default_factory=GengMoegirlSection)
    gengsearx: GengSearxSection = Field(default_factory=GengSearxSection)
    gengplay: GengPlaySection = Field(default_factory=GengPlaySection)
    gengpush: GengPushSection = Field(default_factory=GengPushSection)

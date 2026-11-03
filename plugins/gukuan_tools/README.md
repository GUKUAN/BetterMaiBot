# 麦麦工具箱（gukuan.tools）

自研杂项插件，v3 起 manager 化并入 BetterMaiBot 主仓库。

## 功能

| 功能 | 目录 | 干什么 |
|------|------|--------|
| guard 归属守卫 | `guard/` | 回复正文 @ 的人和引用对象不符时清掉 @；锚点挂错了改挂相关性更高的那条 |
| gossip 瓜报 | `gossip/` | 群里有人骂她或说主人坏话，词表初筛 → LLM 打分 → 私聊主人告状，瓜记 SQLite |
| meal 饭票 | `meal/` | 饭点撒娇要外卖，主人答应后演「到啦」「吃完了」，带虚拟账本和每月预算 |
| geng 新梗 | `geng/` | 每天从萌百 sitemap 差集和 searxng 捞新梗，蒸馏进全局黑话库，顺口的主动抛一次 |

## 结构

```
plugin.py            SDK 入口：hook/command/tool 装饰器 + 一行转发（≈150 行，没有业务）
core/context.py      PluginContext：功能公用的发送/注入/后台任务/生活状态查询
core/registry.py     FeatureRegistry：功能注册表（setup/teardown/reload 生命周期）
config_sections.py   配置段定义
guard/manager.py     AtGuardManager
gossip/manager.py    GossipManager
meal/manager.py      MealManager（调度 + 起单 + 推进 + planner 注入）
meal/menu.py         菜单解析与「买/不买」判定
meal/store.py        state.json 持久层（原子写）
meal/blocks.py       planner 注入块
meal/phrases.py      话术模板（LLM 挂了用兜底台词）
geng/manager.py      GengManager（调度 + hook + 指令逻辑）
geng/collector.py    采集流水线（萌百/searxng/蒸馏/推送，纯函数，db_path 由参数传入）
geng/store.py        SQLite 梗库（titles/terms/pending/play 四张表）
```

依赖方向和反作弊项目一个规矩：`plugin.py → registry → 各 Manager → core/context + 各自的领域模块`，
领域模块（menu/store/collector/blocks/phrases）不 import 回 Manager。

## 加新功能

1. 建目录写 `manager.py`，类里持 `core`（PluginContext），实现 `setup/teardown/reload`
2. plugin.py 里 `registry.register(XxxManager(self.core))`
3. SDK 的 hook/command 装饰器加在 plugin.py，方法体一行转发

## 指令

投喂 / 账本 / 饭史 / 饭开关 —— 饭票
梗 / 梗跑 / 梗冷启动 / 梗找 / 梗审 / 梗收 / 梗弃 / 梗玩 / 梗开关 —— 新梗

全部 operator 权限。

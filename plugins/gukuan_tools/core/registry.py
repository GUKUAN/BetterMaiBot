"""功能注册表

每个功能实现 Feature 协议，在 plugin.py 里 register 进来。
SDK 的装饰器只能挂在插件类的方法上，所以 hook/command/tool 的入口留在
plugin.py，内部转发到这里登记的功能 —— 外面看是 SDK 的形状，里面是注册式。
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Feature(Protocol):
    """一个功能模块的最小契约。name 用于注册表寻址和日志前缀。"""

    name: str

    async def setup(self) -> None:
        """on_load 时调用：建库、起后台循环。"""

    async def teardown(self) -> None:
        """on_unload 时调用：停循环、落盘。"""

    def reload(self) -> None:
        """配置变更时调用（可选实现）。"""


class FeatureRegistry:
    def __init__(self) -> None:
        self._features: dict[str, Any] = {}

    def register(self, feature: Any) -> None:
        self._features[feature.name] = feature

    def get(self, name: str) -> Any:
        return self._features.get(name)

    @property
    def all(self) -> list[Any]:
        return list(self._features.values())

    async def setup_all(self) -> None:
        for feature in self._features.values():
            await feature.setup()

    async def teardown_all(self) -> None:
        for feature in reversed(self._features.values()):
            try:
                await feature.teardown()
            except Exception as exc:  # 卸载是收尾，一个挂了不连累别的
                print(f"[gukuan_tools] {feature.name} 卸载出错: {exc}")

    def reload_all(self) -> None:
        for feature in self._features.values():
            reload = getattr(feature, "reload", None)
            if callable(reload):
                reload()

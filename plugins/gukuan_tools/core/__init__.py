"""core —— 公共运行环境与功能注册表"""

from .context import PluginContext, PLUGIN_ID, sid_of
from .registry import Feature, FeatureRegistry

__all__ = ["PluginContext", "PLUGIN_ID", "sid_of", "Feature", "FeatureRegistry"]

"""
智能流量调节插件 (Smart Flow Controller)

核心任务：监控账号的发信烈度，当短时间内的并发请求超过阈值时，
自动将并行发送转为串行队列，以规避风控。
"""

from src.plugin_system.base.plugin_metadata import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="smart_flow_controller",
    description="智能流量调节插件 - 监控发信烈度，自动切换并行/串行发送模式以规避风控",
    usage="插件自动工作，通过钩子拦截所有消息发送，根据烈度自动调节发送节奏。",
    version="1.0.0",
    author="言柒",
    license="GPL-v3.0-or-later",
    repository_url="https://github.com/tt-P607/smart_flow_controller",
    keywords=["flow-control", "rate-limit", "anti-ban", "queue", "throttle"],
    categories=["system", "protection"],
    extra={
        "is_built_in": True,
    },
)
"""
智能流量调节插件 (Smart Flow Controller)

核心任务：监控账号的发信烈度，当短时间内的并发请求超过阈值时，
自动将并行发送转为串行队列，以规避风控。
"""

from __future__ import annotations

from typing import ClassVar

from src.common.logger import get_logger
from src.plugin_system import ConfigField, register_plugin
from src.plugin_system.apis import config_api
from src.plugin_system.base import BasePlugin

from .src import FlowController, FlowControllerConfig, get_flow_controller, set_flow_controller

logger = get_logger("smart_flow_controller", color="#00D7AF", alias="智能流控")


@register_plugin
class SmartFlowControllerPlugin(BasePlugin):
    """智能流量调节插件"""
    
    plugin_name = "smart_flow_controller"
    config_file_name = "config.toml"
    enable_plugin = True
    plugin_version = "1.0.0"
    plugin_author = "MoFox Team"
    plugin_description = "智能流量调节插件 - 监控发信烈度，自动切换并行/串行发送模式以规避风控"
    
    config_section_descriptions: ClassVar = {
        "plugin": "插件开关",
        "flow_control": "流量控制配置",
        "timing": "时间配置",
        "private_priority": "私聊优先配置",
        "log": "日志配置",
    }
    
    config_schema: ClassVar[dict] = {
        "plugin": {
            "enabled": ConfigField(
                type=bool,
                default=True,
                description="是否启用智能流量调节插件"
            ),
            "config_version": ConfigField(
                type=str,
                default="1.0.0",
                description="配置文件版本"
            ),
        },
        "flow_control": {
            "enabled": ConfigField(
                type=bool,
                default=True,
                description="是否启用流量控制"
            ),
            "normal_threshold": ConfigField(
                type=int,
                default=10,
                description="正常模式阈值（条/分钟）- 超过此值进入节流模式"
            ),
            "throttled_threshold": ConfigField(
                type=int,
                default=20,
                description="节流模式阈值（条/分钟）- 超过此值进入危急模式"
            ),
        },
        "timing": {
            "min_interval": ConfigField(
                type=float, 
                default=0.5, 
                description="节流模式下最小消息间隔（秒）"
            ),
            "breathing_interval": ConfigField(
                type=int, 
                default=3, 
                description="连续发送几条后增加喘息间隔"
            ),
        },
        "private_priority": {
            "enabled": ConfigField(
                type=bool,
                default=True,
                description="是否启用私聊优先"
            ),
            "max_wait": ConfigField(
                type=float,
                default=3.0,
                description="私聊最大等待时间（秒）- 超过此时间强制发送"
            ),
        },
        "log": {
            "debug_log": ConfigField(
                type=bool,
                default=False,
                description="是否显示详细调试日志（默认关闭，只显示简洁日志）"
            ),
        },
    }
        
    async def on_plugin_loaded(self) -> None:
        """插件加载时初始化"""
        logger.info("[SmartFlowController] 插件正在加载...")
        
        # 初始化实例变量
        self._flow_controller: FlowController | None = None
        
        # 读取配置
        config = self._build_config()
        
        if not config.enabled:
            logger.info("[SmartFlowController] 流量控制已禁用，跳过初始化")
            return
            
        # 创建流量控制器
        self._flow_controller = FlowController(config)
        set_flow_controller(self._flow_controller)
        
        # 安装钩子
        self._flow_controller.install_hook()
        
        # 启动控制器
        await self._flow_controller.start()
        
        logger.info("[SmartFlowController] 插件加载完成")
        
    def on_unload(self) -> None:
        """插件卸载时清理（同步方法）"""
        logger.info("[SmartFlowController] 插件正在卸载...")
        
        # 获取当前事件循环
        import asyncio
        
        if hasattr(self, '_flow_controller') and self._flow_controller:
            try:
                # 尝试在当前事件循环中运行异步关闭
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # 创建任务来异步关闭
                    asyncio.create_task(self._async_shutdown())
                else:
                    loop.run_until_complete(self._flow_controller.shutdown())
            except RuntimeError:
                # 如果没有事件循环，直接恢复原始函数
                self._flow_controller.restore_original_send()
            finally:
                set_flow_controller(None)
                self._flow_controller = None
            
        logger.info("[SmartFlowController] 插件卸载完成")
        
    async def _async_shutdown(self) -> None:
        """异步关闭流量控制器"""
        if hasattr(self, '_flow_controller') and self._flow_controller:
            await self._flow_controller.shutdown()
            set_flow_controller(None)
            self._flow_controller = None
        
    def _build_config(self) -> FlowControllerConfig:
        """从插件配置构建 FlowControllerConfig
        
        Returns:
            FlowControllerConfig 实例
        """
        # 基础配置
        enabled = config_api.get_plugin_config(
            self.config, "flow_control.enabled", True
        )
        normal_threshold = config_api.get_plugin_config(
            self.config, "flow_control.normal_threshold", 10
        )
        throttled_threshold = config_api.get_plugin_config(
            self.config, "flow_control.throttled_threshold", 20
        )
        
        # 时间配置
        min_interval = config_api.get_plugin_config(
            self.config, "timing.min_interval", 0.5
        )
        breathing_interval = config_api.get_plugin_config(
            self.config, "timing.breathing_interval", 3
        )
        
        # 私聊优先配置
        private_priority_enabled = config_api.get_plugin_config(
            self.config, "private_priority.enabled", True
        )
        private_max_wait = config_api.get_plugin_config(
            self.config, "private_priority.max_wait", 3.0
        )
        
        # 日志配置
        debug_log = config_api.get_plugin_config(
            self.config, "log.debug_log", False
        )
        
        config = FlowControllerConfig(
            enabled=enabled,
            debug_log=debug_log,
            normal_threshold=normal_threshold,
            throttled_threshold=throttled_threshold,
            throttled_base_delay=min_interval,
            breathing_interval=breathing_interval,
            private_priority_enabled=private_priority_enabled,
            private_max_wait=private_max_wait,
        )
        
        logger.info(
            f"[SmartFlowController] 配置加载完成 | "
            f"启用: {enabled} | "
            f"正常阈值: {normal_threshold} | "
            f"节流阈值: {throttled_threshold} | "
            f"私聊优先: {private_priority_enabled}"
        )
        
        return config
        
    def get_plugin_components(self) -> list:
        """返回插件组件（本插件无额外组件）"""
        return []
        
    @property
    def status(self) -> dict:
        """获取插件状态"""
        if self._flow_controller:
            return self._flow_controller.status
        return {
            "enabled": False,
            "running": False,
            "message": "流量控制器未初始化",
        }
"""
智能流量调节插件 - 核心模块

导出所有核心组件供外部使用
"""

from .models import (
    FlowMode,
    MessagePriority,
    SendRecord,
    IntensitySnapshot,
    QueuedMessage,
    FlowControllerConfig,
)
from .flow_controller import FlowController, get_flow_controller, set_flow_controller
from .intensity_monitor import IntensityMonitor
from .priority_queue import AdaptivePriorityQueue
from .breathing_pacer import BreathingPacer

__all__ = [
    # 枚举
    "FlowMode",
    "MessagePriority",
    # 数据类
    "SendRecord",
    "IntensitySnapshot",
    "QueuedMessage",
    "FlowControllerConfig",
    # 核心组件
    "FlowController",
    "get_flow_controller",
    "set_flow_controller",
    "IntensityMonitor",
    "AdaptivePriorityQueue",
    "BreathingPacer",
]
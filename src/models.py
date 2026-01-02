"""
智能流量调节插件 - 数据模型
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine
import asyncio
import time


class FlowMode(Enum):
    """流量控制模式"""
    NORMAL = "normal"          # 正常模式
    THROTTLED = "throttled"    # 节流模式
    CRITICAL = "critical"      # 危急模式


class MessagePriority(Enum):
    """消息优先级"""
    PRIVATE = 100      # 私聊最高优先级
    GROUP_REPLY = 50   # 群聊回复中等优先级
    GROUP_NORMAL = 10  # 群聊普通消息低优先级


@dataclass
class SendRecord:
    """发送记录"""
    timestamp: float           # 发送时间戳
    is_private: bool           # 是否为私聊
    stream_id: str             # 聊天流ID
    
    def is_expired(self, window_seconds: float) -> bool:
        """检查记录是否已过期"""
        return time.time() - self.timestamp > window_seconds


@dataclass
class IntensitySnapshot:
    """烈度快照"""
    total_count_1min: int      # 过去1分钟总发信量
    private_count_1min: int    # 过去1分钟私聊发信量
    group_count_1min: int      # 过去1分钟群聊发信量
    pending_private: int       # 待发私聊消息数
    pending_group: int         # 待发群聊消息数
    current_mode: FlowMode     # 当前模式


@dataclass
class QueuedMessage:
    """队列中的消息"""
    envelope: dict                          # MessageEnvelope
    chat_stream: Any                        # ChatStream | None
    db_message: Any                         # DatabaseMessages | None
    show_log: bool                          # 是否显示日志
    priority: MessagePriority               # 优先级
    enqueue_time: float                     # 入队时间
    future: asyncio.Future = field(default_factory=asyncio.Future)  # 等待结果的 Future
    stream_id: str = ""                     # 聊天流ID
    is_private: bool = False                # 是否为私聊
    original_send: Callable[[], Coroutine[Any, Any, bool]] | None = None  # 原始发送函数
    batch_id: str | None = None             # 批次ID（用于分段消息保护）
    
    def __post_init__(self):
        if not self.stream_id and self.chat_stream:
            self.stream_id = getattr(self.chat_stream, "stream_id", "unknown")


@dataclass
class FlowControllerConfig:
    """流量控制器配置"""
    # 基础配置
    enabled: bool = True
    debug_log: bool = False  # 是否显示详细调试日志
    
    # 流量阈值
    normal_threshold: int = 10           # 正常模式阈值（条/分钟）
    throttled_threshold: int = 20        # 节流模式阈值（条/分钟）
    queue_critical_threshold: int = 10   # 队列积压触发危急模式的阈值
    
    # 时间配置
    min_interval: float = 0.5            # 节流模式下最小消息间隔（秒）
    breathing_interval: int = 3          # 连续发送几条后喘息
    
    # 私聊优先配置
    private_priority_enabled: bool = True
    private_max_wait: float = 3.0        # 私聊最大等待时间（秒）
    
    # 抖动配置（内部自动计算，但可覆盖）
    normal_jitter_min: float = 0.1
    normal_jitter_max: float = 0.5
    throttled_jitter_min: float = 0.3
    throttled_jitter_max: float = 1.0
    critical_jitter_min: float = 0.5
    critical_jitter_max: float = 2.0
    
    # 呼吸暂停配置
    breathing_pause_min: float = 1.5
    breathing_pause_max: float = 2.5
    
    # 最小间隔（毫秒）- 确保无重合时间戳
    min_gap_ms: int = 50
    
    # 基础延迟配置
    normal_base_delay: float = 0.0
    throttled_base_delay: float = 0.5
    critical_base_delay: float = 2.0
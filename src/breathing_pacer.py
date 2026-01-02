"""
智能流量调节插件 - 呼吸节拍器

职责：计算每条消息发送前的等待时间，实现"呼吸感"
"""

import random
import time

from src.common.logger import get_logger

from .models import FlowControllerConfig, FlowMode

logger = get_logger("flow_controller.breathing", color="#AFFF87", alias="呼吸节拍")


def _mode_colored(mode: FlowMode) -> str:
    """根据模式返回带 Rich 标记颜色的模式名称"""
    if mode == FlowMode.NORMAL:
        return f"[green]{mode.value}[/green]"
    elif mode == FlowMode.THROTTLED:
        return f"[yellow]{mode.value}[/yellow]"
    else:  # CRITICAL
        return f"[bold red]{mode.value}[/bold red]"


class BreathingPacer:
    """呼吸节拍器 - 模拟人类自然的发送节奏"""
    
    def __init__(self, config: FlowControllerConfig):
        self.config = config
        self._consecutive_count: int = 0
        self._last_send_time: float = 0.0
        
    def calculate_delay(self, flow_mode: FlowMode, is_private: bool, debug_log: bool = False) -> float:
        """计算下一条消息的延迟时间
        
        Args:
            flow_mode: 当前流量模式
            is_private: 是否为私聊消息
            debug_log: 是否输出详细调试日志
            
        Returns:
            延迟时间（秒）
        """
        base_delay = self._get_base_delay(flow_mode, is_private)
        jitter = self._calculate_jitter(flow_mode)
        breathing_pause = self._calculate_breathing_pause(debug_log)
        
        total_delay = base_delay + jitter + breathing_pause
        
        # 确保最小间隔（消除重合时间戳）
        elapsed = time.time() - self._last_send_time
        min_gap = self.config.min_gap_ms / 1000.0
        
        if elapsed < min_gap:
            total_delay += (min_gap - elapsed)
        
        # 只在调试模式下输出详细日志
        if debug_log:
            mode_str = _mode_colored(flow_mode)
            logger.info(
                f"计算延迟 | "
                f"模式: {mode_str} | "
                f"类型: {'私聊' if is_private else '群聊'} | "
                f"基础: {base_delay:.2f}s | "
                f"抖动: {jitter:.2f}s | "
                f"喘息: {breathing_pause:.2f}s | "
                f"总计: {total_delay:.2f}s"
            )
        
        return total_delay
    
    def _get_base_delay(self, flow_mode: FlowMode, is_private: bool) -> float:
        """获取基础延迟
        
        Args:
            flow_mode: 当前流量模式
            is_private: 是否为私聊消息
            
        Returns:
            基础延迟时间（秒）
        """
        if flow_mode == FlowMode.NORMAL:
            # 正常模式：私聊无延迟，群聊有基础延迟
            return 0.0 if is_private else self.config.normal_base_delay
        elif flow_mode == FlowMode.THROTTLED:
            return self.config.throttled_base_delay
        else:  # CRITICAL
            return self.config.critical_base_delay
    
    def _calculate_jitter(self, flow_mode: FlowMode) -> float:
        """计算随机抖动
        
        Args:
            flow_mode: 当前流量模式
            
        Returns:
            抖动时间（秒）
        """
        if flow_mode == FlowMode.NORMAL:
            return random.uniform(
                self.config.normal_jitter_min,
                self.config.normal_jitter_max
            )
        elif flow_mode == FlowMode.THROTTLED:
            return random.uniform(
                self.config.throttled_jitter_min,
                self.config.throttled_jitter_max
            )
        else:  # CRITICAL
            return random.uniform(
                self.config.critical_jitter_min,
                self.config.critical_jitter_max
            )
    
    def _calculate_breathing_pause(self, debug_log: bool = False) -> float:
        """计算呼吸暂停 - 连续发送后增加间隔
        
        Args:
            debug_log: 是否输出详细调试日志
            
        Returns:
            呼吸暂停时间（秒）
        """
        self._consecutive_count += 1
        
        # 每连续发送 N 条后，增加一个"喘息"间隔
        if self._consecutive_count >= self.config.breathing_interval:
            self._consecutive_count = 0
            pause = random.uniform(
                self.config.breathing_pause_min,
                self.config.breathing_pause_max
            )
            if debug_log:
                logger.info(f"触发喘息暂停: {pause:.2f}s")
            return pause
        
        return 0.0
    
    def record_send(self) -> None:
        """记录发送完成"""
        self._last_send_time = time.time()
        
    def reset_consecutive(self) -> None:
        """重置连续发送计数（例如：长时间无发送后）"""
        self._consecutive_count = 0
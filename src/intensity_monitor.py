"""
智能流量调节插件 - 烈度监控器

职责：实时统计发信烈度，判断当前系统负载状态
"""

from collections import deque
import time

from src.common.logger import get_logger

from .models import FlowControllerConfig, FlowMode, IntensitySnapshot, SendRecord

logger = get_logger("flow_controller.intensity", color="#5FD7AF", alias="烈度监控")


def _mode_colored(mode: FlowMode) -> str:
    """根据模式返回带 Rich 标记颜色的模式名称
    
    - NORMAL: 绿色（舒适）
    - THROTTLED: 黄色（警告）
    - CRITICAL: 红色（危急）
    """
    if mode == FlowMode.NORMAL:
        return f"[green]{mode.value}[/green]"
    elif mode == FlowMode.THROTTLED:
        return f"[yellow]{mode.value}[/yellow]"
    else:  # CRITICAL
        return f"[bold red]{mode.value}[/bold red]"


class IntensityMonitor:
    """烈度监控器 - 实时统计发信烈度"""
    
    def __init__(self, config: FlowControllerConfig):
        self.config = config
        self._send_records: deque[SendRecord] = deque()
        self._window_seconds: float = 60.0  # 1分钟窗口
        self._pending_private: int = 0
        self._pending_group: int = 0
        self._last_mode: FlowMode = FlowMode.NORMAL
        
    def record_send(self, is_private: bool, stream_id: str = "", debug_log: bool = False) -> None:
        """记录一次发送
        
        Args:
            is_private: 是否为私聊消息
            stream_id: 聊天流ID
            debug_log: 是否输出详细调试日志
        """
        record = SendRecord(
            timestamp=time.time(),
            is_private=is_private,
            stream_id=stream_id,
        )
        self._send_records.append(record)
        
        # 清理过期记录
        self._cleanup_expired()
        
        # 只在调试模式下输出详细日志
        if debug_log:
            snapshot = self.get_snapshot()
            mode_str = _mode_colored(snapshot.current_mode)
            logger.info(
                f"发送记录 | "
                f"类型: {'私聊' if is_private else '群聊'} | "
                f"1分钟内: {snapshot.total_count_1min}条 | "
                f"模式: {mode_str}"
            )
        
    def _cleanup_expired(self) -> None:
        """清理过期的发送记录"""
        cutoff = time.time() - self._window_seconds
        while self._send_records and self._send_records[0].timestamp < cutoff:
            self._send_records.popleft()
            
    def get_snapshot(self) -> IntensitySnapshot:
        """获取当前烈度快照"""
        self._cleanup_expired()
        
        private_count = sum(1 for r in self._send_records if r.is_private)
        group_count = len(self._send_records) - private_count
        
        return IntensitySnapshot(
            total_count_1min=len(self._send_records),
            private_count_1min=private_count,
            group_count_1min=group_count,
            pending_private=self._pending_private,
            pending_group=self._pending_group,
            current_mode=self.get_flow_mode(),
        )
        
    def get_flow_mode(self) -> FlowMode:
        """根据烈度判断当前流量模式"""
        self._cleanup_expired()
        
        total_count = len(self._send_records)
        total_pending = self._pending_private + self._pending_group
        
        # 判断模式
        new_mode: FlowMode
        if total_count >= self.config.throttled_threshold or total_pending > self.config.queue_critical_threshold:
            new_mode = FlowMode.CRITICAL
        elif total_count >= self.config.normal_threshold:
            new_mode = FlowMode.THROTTLED
        else:
            new_mode = FlowMode.NORMAL
            
        # 模式变化时记录日志
        if new_mode != self._last_mode:
            old_str = _mode_colored(self._last_mode)
            new_str = _mode_colored(new_mode)
            
            # 根据新模式选择日志级别
            if new_mode == FlowMode.CRITICAL:
                logger.warning(
                    f"⚠️ 模式切换: {old_str} -> {new_str} | "
                    f"1分钟发送: {total_count}条 | 队列积压: {total_pending}条"
                )
            elif new_mode == FlowMode.THROTTLED:
                logger.info(
                    f"⚡ 模式切换: {old_str} -> {new_str} | "
                    f"1分钟发送: {total_count}条 | 队列积压: {total_pending}条"
                )
            else:
                logger.info(
                    f"✅ 模式切换: {old_str} -> {new_str} | "
                    f"1分钟发送: {total_count}条 | 队列积压: {total_pending}条"
                )
            self._last_mode = new_mode
            
        return new_mode
        
    def update_pending_counts(self, pending_private: int, pending_group: int) -> None:
        """更新待发消息数量
        
        Args:
            pending_private: 待发私聊消息数
            pending_group: 待发群聊消息数
        """
        self._pending_private = pending_private
        self._pending_group = pending_group
        
    def get_count_1min(self) -> int:
        """获取1分钟内的发送总量"""
        self._cleanup_expired()
        return len(self._send_records)
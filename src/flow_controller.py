"""
智能流量调节插件 - 流量控制器

核心控制器单例，负责：
1. 管理消息队列
2. 协调各模块工作
3. 提供钩子注入/恢复功能
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from src.common.logger import get_logger

from .breathing_pacer import BreathingPacer
from .intensity_monitor import IntensityMonitor
from .models import FlowControllerConfig, FlowMode, MessagePriority, QueuedMessage
from .priority_queue import AdaptivePriorityQueue

if TYPE_CHECKING:
    from mofox_wire import MessageEnvelope
    from src.chat.message_receive.chat_stream import ChatStream
    from src.common.data_models.database_data_model import DatabaseMessages

logger = get_logger("flow_controller", color="#00D7AF", alias="流量控制")


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


# 全局单例
_flow_controller: "FlowController | None" = None


def get_flow_controller() -> "FlowController | None":
    """获取流量控制器单例"""
    return _flow_controller


def set_flow_controller(controller: "FlowController | None") -> None:
    """设置流量控制器单例"""
    global _flow_controller
    _flow_controller = controller


class FlowController:
    """流量控制器 - 全局单例"""
    
    def __init__(self, config: FlowControllerConfig):
        self.config = config
        self.intensity_monitor = IntensityMonitor(config)
        self.priority_queue = AdaptivePriorityQueue(config)
        self.breathing_pacer = BreathingPacer(config)
        
        self._send_lock = asyncio.Lock()  # 全局发送锁
        self._running = False
        self._accepting_new = True
        self._worker_task: asyncio.Task | None = None
        self._timeout_checker_task: asyncio.Task | None = None
        
        # 保存原始发送函数
        self._original_send_envelope: Callable[..., Coroutine[Any, Any, bool]] | None = None
        
        logger.info(
            f"[FlowController] 初始化完成 | "
            f"正常阈值: {config.normal_threshold} | "
            f"节流阈值: {config.throttled_threshold} | "
            f"私聊优先: {config.private_priority_enabled}"
        )
        
    async def start(self) -> None:
        """启动流量控制器"""
        if self._running:
            logger.warning("[FlowController] 已在运行中，跳过启动")
            return
            
        self._running = True
        self._accepting_new = True
        
        # 启动队列工作器
        self._worker_task = asyncio.create_task(self._worker_loop())
        
        # 启动私聊超时检查器
        if self.config.private_priority_enabled:
            self._timeout_checker_task = asyncio.create_task(self._timeout_checker_loop())
        
        logger.info("[FlowController] 已启动")
        
    async def stop(self) -> None:
        """停止流量控制器"""
        if not self._running:
            return
            
        logger.info("[FlowController] 开始停止...")
        self._running = False
        
        # 取消任务
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
                
        if self._timeout_checker_task:
            self._timeout_checker_task.cancel()
            try:
                await self._timeout_checker_task
            except asyncio.CancelledError:
                pass
                
        logger.info("[FlowController] 已停止")
        
    async def shutdown(self) -> None:
        """优雅关闭 - 等待队列清空后停止"""
        logger.info("[FlowController] 开始优雅关闭...")
        
        # 停止接收新消息
        self._accepting_new = False
        
        # 等待队列清空（最多30秒）
        deadline = time.time() + 30.0
        while not self.priority_queue.is_empty() and time.time() < deadline:
            await asyncio.sleep(0.1)
            
        if not self.priority_queue.is_empty():
            pending = self.priority_queue.get_pending_count()
            logger.warning(f"[FlowController] 关闭超时，仍有 {pending} 条消息未发送")
            
        # 停止工作器
        await self.stop()
        
        # 恢复原始函数
        self.restore_original_send()
        
        logger.info("[FlowController] 已优雅关闭")
        
    def is_enabled(self) -> bool:
        """检查流量控制是否启用"""
        return self.config.enabled and self._running
        
    async def enqueue(self, message: QueuedMessage) -> None:
        """将消息加入队列
        
        Args:
            message: 要加入队列的消息
        """
        if not self._accepting_new:
            logger.warning("已停止接收新消息，直接发送")
            if message.original_send:
                result = await message.original_send()
                if not message.future.done():
                    message.future.set_result(result)
            return
            
        await self.priority_queue.enqueue(message, self.config.debug_log)
        
        # 更新烈度监控器的待发消息数
        stats = self.priority_queue.get_stats()
        self.intensity_monitor.update_pending_counts(
            stats["pending_private"],
            stats["pending_group"]
        )
        
    async def _worker_loop(self) -> None:
        """队列工作器主循环"""
        if self.config.debug_log:
            logger.info("[FlowController] 队列工作器已启动")
        
        while self._running:
            try:
                # 获取下一条待发消息
                queued_msg = await self.priority_queue.dequeue(timeout=1.0, debug_log=self.config.debug_log)
                
                if queued_msg is None:
                    continue
                    
                # 计算延迟
                flow_mode = self.intensity_monitor.get_flow_mode()
                delay = self.breathing_pacer.calculate_delay(
                    flow_mode,
                    queued_msg.is_private,
                    self.config.debug_log
                )
                
                # 获取统计信息
                stats = self.priority_queue.get_stats()
                mode_str = _mode_colored(flow_mode)
                msg_type = "私聊" if queued_msg.is_private else "群聊"
                
                # 简洁日志：一行显示所有关键信息
                logger.info(
                    f"📤 {msg_type} | 队列: {stats['total_pending']+1} | "
                    f"等待: {delay:.1f}s | 模式: {mode_str}"
                )
                
                # 等待
                if delay > 0:
                    await asyncio.sleep(delay)
                
                # 执行发送
                success = False
                async with self._send_lock:
                    if queued_msg.original_send:
                        try:
                            success = await queued_msg.original_send()
                        except Exception as e:
                            logger.error(f"发送失败: {e}")
                            success = False
                    else:
                        logger.error("消息缺少 original_send 函数")
                        
                # 记录发送（静默模式，不输出日志）
                self.intensity_monitor.record_send(
                    queued_msg.is_private,
                    queued_msg.stream_id,
                    self.config.debug_log
                )
                self.breathing_pacer.record_send()
                
                # 更新待发消息数
                stats = self.priority_queue.get_stats()
                self.intensity_monitor.update_pending_counts(
                    stats["pending_private"],
                    stats["pending_group"]
                )
                
                # 完成 Future
                if not queued_msg.future.done():
                    queued_msg.future.set_result(success)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"队列工作器错误: {e}")
                await asyncio.sleep(0.5)
                
        if self.config.debug_log:
            logger.info("[FlowController] 队列工作器已停止")
        
    async def _timeout_checker_loop(self) -> None:
        """私聊超时检查器循环"""
        logger.info("[FlowController] 私聊超时检查器已启动")
        
        while self._running:
            try:
                await asyncio.sleep(0.5)
                
                # 检查超时的私聊消息
                timeout_messages = await self.priority_queue.check_private_timeout()
                
                for msg in timeout_messages:
                    logger.warning(
                        f"[FlowController] 私聊消息超时，立即发送 | "
                        f"stream: {msg.stream_id}"
                    )
                    
                    # 立即发送超时消息
                    async with self._send_lock:
                        if msg.original_send:
                            try:
                                success = await msg.original_send()
                                self.intensity_monitor.record_send(True, msg.stream_id)
                                self.breathing_pacer.record_send()
                                if not msg.future.done():
                                    msg.future.set_result(success)
                            except Exception as e:
                                logger.error(f"[FlowController] 超时消息发送失败: {e}")
                                if not msg.future.done():
                                    msg.future.set_result(False)
                                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[FlowController] 超时检查器错误: {e}")
                
        logger.info("[FlowController] 私聊超时检查器已停止")
        
    def install_hook(self) -> None:
        """安装 send_envelope 钩子"""
        from src.chat.message_receive import uni_message_sender
        
        # 保存原始函数
        self._original_send_envelope = uni_message_sender.send_envelope
        
        # 创建钩子函数
        async def hooked_send_envelope(
            envelope: "MessageEnvelope",
            chat_stream: "ChatStream | None" = None,
            db_message: "DatabaseMessages | None" = None,
            show_log: bool = True,
        ) -> bool:
            """被钩子包装的 send_envelope"""
            controller = get_flow_controller()
            
            if not controller or not controller.is_enabled():
                # 流量控制未启用，直接发送
                if self._original_send_envelope:
                    return await self._original_send_envelope(envelope, chat_stream, db_message, show_log)
                return False
            
            # 判断是否为私聊
            is_private = self._is_private_message(envelope, chat_stream)
            
            # 创建 Future 用于等待发送完成
            future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
            
            # 获取 stream_id
            stream_id = ""
            if chat_stream:
                stream_id = getattr(chat_stream, "stream_id", "unknown")
            
            # 创建原始发送函数的闭包
            original_send_envelope = self._original_send_envelope
            async def do_send() -> bool:
                if original_send_envelope:
                    return await original_send_envelope(envelope, chat_stream, db_message, show_log)
                return False
            
            # 检查是否为命令响应 (特快列车)
            if controller._is_command_context():
                if show_log and controller.config.debug_log:
                    logger.info("⚡ 检测到命令响应，跳过流控直接发送")
                return await do_send()
            
            # 入队
            queued_msg = QueuedMessage(
                envelope=dict(envelope) if hasattr(envelope, 'items') else envelope,  # type: ignore
                chat_stream=chat_stream,
                db_message=db_message,
                show_log=show_log,
                priority=MessagePriority.PRIVATE if is_private else MessagePriority.GROUP_NORMAL,
                enqueue_time=time.time(),
                future=future,
                stream_id=stream_id,
                is_private=is_private,
                original_send=do_send,
            )
            
            await controller.enqueue(queued_msg)
            
            # 等待发送完成
            return await future
        
        # 替换原始函数
        uni_message_sender.send_envelope = hooked_send_envelope
        
        logger.info("[FlowController] 已安装 send_envelope 钩子")
        
    def restore_original_send(self) -> None:
        """恢复原始的 send_envelope 函数"""
        if self._original_send_envelope:
            from src.chat.message_receive import uni_message_sender
            uni_message_sender.send_envelope = self._original_send_envelope
            logger.info("[FlowController] 已恢复原始 send_envelope 函数")
            
    def _is_private_message(
        self, 
        envelope: "MessageEnvelope", 
        chat_stream: "ChatStream | None"
    ) -> bool:
        """判断消息是否为私聊
        
        Args:
            envelope: 消息信封
            chat_stream: 聊天流
            
        Returns:
            如果是私聊消息返回 True
        """
        # 优先从 chat_stream 判断
        if chat_stream:
            # 检查 group_info 属性
            group_info = getattr(chat_stream, "group_info", None)
            if group_info is None:
                return True
            # 检查 group_id
            group_id = getattr(group_info, "group_id", None)
            if group_id is None:
                return True
            return False
            
        # 从 envelope 判断
        if hasattr(envelope, "get"):
            # 检查 target_id 格式或其他标识
            target_id = envelope.get("target_id", "")
            if target_id and "group" not in str(target_id).lower():
                return True
                
        return False
        
    def _is_command_context(self) -> bool:
        """检查当前是否处于命令执行上下文中
        
        通过检查调用栈中是否存在 PlusCommand 的实例来判断。
        """
        try:
            # 获取当前栈帧
            current_frame = inspect.currentframe()
            frame = current_frame
            # 限制检查深度，避免性能开销过大
            depth = 0
            while frame and depth < 20:
                # 检查局部变量中的 'self'
                instance = frame.f_locals.get('self')
                if instance:
                    # 检查实例的类是否继承自 PlusCommand
                    # 为了不引入 PlusCommand 的依赖，我们检查类名或其基类名
                    for cls in inspect.getmro(instance.__class__):
                        if cls.__name__ == 'PlusCommand':
                            return True
                
                frame = frame.f_back
                depth += 1
            return False
        except Exception as e:
            # 任何异常都视为非命令，保证不影响正常流程
            logger.warning(f"检查命令上下文失败: {e}")
            return False
        finally:
            del frame # 避免循环引用

    @property
    def status(self) -> dict:
        """获取流量控制器状态"""
        snapshot = self.intensity_monitor.get_snapshot()
        queue_stats = self.priority_queue.get_stats()
        
        return {
            "enabled": self.is_enabled(),
            "running": self._running,
            "accepting_new": self._accepting_new,
            "flow_mode": snapshot.current_mode.value,
            "intensity": {
                "total_1min": snapshot.total_count_1min,
                "private_1min": snapshot.private_count_1min,
                "group_1min": snapshot.group_count_1min,
            },
            "queue": queue_stats,
            "config": {
                "normal_threshold": self.config.normal_threshold,
                "throttled_threshold": self.config.throttled_threshold,
                "private_max_wait": self.config.private_max_wait,
            },
        }
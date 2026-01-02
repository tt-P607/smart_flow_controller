"""
智能流量调节插件 - 优先级队列

职责：管理待发消息队列，实现私聊优先
"""

import asyncio
from typing import Optional
import time

from src.common.logger import get_logger

from .models import FlowControllerConfig, QueuedMessage

logger = get_logger("flow_controller.queue", color="#87D7AF", alias="发送队列")


class AdaptivePriorityQueue:
    """自适应优先级队列 - 实现私聊消息优先"""
    
    def __init__(self, config: FlowControllerConfig):
        self.config = config
        self._private_queue: asyncio.Queue[QueuedMessage] = asyncio.Queue()
        self._group_queue: asyncio.Queue[QueuedMessage] = asyncio.Queue()
        self._lock = asyncio.Lock()
        
    async def enqueue(self, message: QueuedMessage, debug_log: bool = False) -> None:
        """将消息加入队列
        
        Args:
            message: 要加入队列的消息
            debug_log: 是否输出详细调试日志
        """
        async with self._lock:
            if message.is_private:
                await self._private_queue.put(message)
                if debug_log:
                    logger.info(
                        f"私聊消息入队 | "
                        f"私聊队列: {self._private_queue.qsize()} | "
                        f"群聊队列: {self._group_queue.qsize()}"
                    )
            else:
                await self._group_queue.put(message)
                if debug_log:
                    logger.info(
                        f"群聊消息入队 | "
                        f"私聊队列: {self._private_queue.qsize()} | "
                        f"群聊队列: {self._group_queue.qsize()}"
                    )
    
    async def dequeue(self, timeout: float = 1.0, debug_log: bool = False) -> Optional[QueuedMessage]:
        """从队列取出下一条消息（私聊优先）
        
        Args:
            timeout: 等待超时时间（秒）
            debug_log: 是否输出详细调试日志
            
        Returns:
            下一条消息，如果超时则返回 None
        """
        # 优先检查私聊队列
        try:
            message = self._private_queue.get_nowait()
            if debug_log:
                logger.info(
                    f"私聊消息出队 | "
                    f"等待时间: {time.time() - message.enqueue_time:.2f}s"
                )
            return message
        except asyncio.QueueEmpty:
            pass
        
        # 私聊队列为空时，从群聊队列取
        try:
            message = await asyncio.wait_for(
                self._group_queue.get(),
                timeout=timeout
            )
            if debug_log:
                logger.info(
                    f"群聊消息出队 | "
                    f"等待时间: {time.time() - message.enqueue_time:.2f}s"
                )
            return message
        except asyncio.TimeoutError:
            return None
            
    async def check_private_timeout(self) -> list[QueuedMessage]:
        """检查并返回超时的私聊消息
        
        Returns:
            超时的私聊消息列表
        """
        timeout_messages: list[QueuedMessage] = []
        now = time.time()
        
        # 暂存未超时的消息
        temp_messages: list[QueuedMessage] = []
        
        async with self._lock:
            while not self._private_queue.empty():
                try:
                    msg = self._private_queue.get_nowait()
                    wait_time = now - msg.enqueue_time
                    
                    if wait_time > self.config.private_max_wait:
                        timeout_messages.append(msg)
                        logger.warning(
                            f"[PriorityQueue] 私聊消息超时 | "
                            f"stream: {msg.stream_id} | "
                            f"等待时间: {wait_time:.2f}s > 阈值: {self.config.private_max_wait}s"
                        )
                    else:
                        temp_messages.append(msg)
                except asyncio.QueueEmpty:
                    break
            
            # 将未超时的消息放回队列
            for msg in temp_messages:
                await self._private_queue.put(msg)
                
        return timeout_messages
        
    def get_stats(self) -> dict:
        """获取队列统计信息
        
        Returns:
            包含队列状态的字典
        """
        return {
            "pending_private": self._private_queue.qsize(),
            "pending_group": self._group_queue.qsize(),
            "total_pending": self._private_queue.qsize() + self._group_queue.qsize(),
        }
        
    def is_empty(self) -> bool:
        """检查队列是否为空
        
        Returns:
            如果两个队列都为空则返回 True
        """
        return self._private_queue.empty() and self._group_queue.empty()
        
    def get_pending_count(self) -> int:
        """获取待处理消息总数
        
        Returns:
            待处理消息总数
        """
        return self._private_queue.qsize() + self._group_queue.qsize()
"""
智能流量调节插件 - 优先级队列

职责：管理待发消息队列，实现私聊优先 + 会话粘性（防打断）
"""

import asyncio
from typing import Optional, Deque
import time
from collections import deque, defaultdict

from src.common.logger import get_logger

from .models import FlowControllerConfig, QueuedMessage

logger = get_logger("flow_controller.queue", color="#87D7AF", alias="发送队列")


class AdaptivePriorityQueue:
    """自适应优先级队列 - 实现私聊消息优先 + 会话粘性"""
    
    STICKY_LIMIT = 3  # 同一会话连续优先处理最大次数

    def __init__(self, config: FlowControllerConfig):
        self.config = config
        
        # 私聊队列：保持简单的 FIFO，因为私聊优先级最高且并发不高
        self._private_queue: asyncio.Queue[QueuedMessage] = asyncio.Queue()
        
        # 群聊队列：按 stream_id 分组的队列
        # key: stream_id, value: deque[QueuedMessage]
        self._group_queues: dict[str, Deque[QueuedMessage]] = defaultdict(deque)
        
        # 活跃的 stream_id 列表 (用于轮询调度)
        self._active_group_streams: Deque[str] = deque()
        
        # 粘性状态记录
        self._last_stream_id: Optional[str] = None
        self._sticky_count: int = 0
        
        self._lock = asyncio.Lock()
        
        # 用于通知有新消息到达的事件
        self._message_event = asyncio.Event()
        
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
                        f"群聊流数: {len(self._group_queues)}"
                    )
            else:
                stream_id = message.stream_id
                
                # 如果该 stream 当前没有积压消息，说明它是新变活跃的，加入调度列表
                if stream_id not in self._group_queues or not self._group_queues[stream_id]:
                    # 只有当它不在活跃列表中时才添加（防止重复，虽然理论上空队列不应在列表中）
                    # 为了保险，先检查。但在正确逻辑下，空队列对应的ID不应存在于 active_streams
                    if stream_id not in self._active_group_streams:
                        self._active_group_streams.append(stream_id)
                
                self._group_queues[stream_id].append(message)
                
                if debug_log:
                    logger.info(
                        f"群聊消息入队 | "
                        f"stream: {stream_id} | "
                        f"当前积压: {len(self._group_queues[stream_id])} | "
                        f"活跃流数: {len(self._active_group_streams)}"
                    )
            
            # 触发有新消息事件，唤醒消费者
            self._message_event.set()
    
    async def dequeue(self, timeout: float = 1.0, debug_log: bool = False) -> Optional[QueuedMessage]:
        """从队列取出下一条消息（私聊优先 -> 粘性群聊 -> 轮询群聊）
        
        Args:
            timeout: 等待超时时间（秒）
            debug_log: 是否输出详细调试日志
            
        Returns:
            下一条消息，如果超时则返回 None
        """
        
        # ==========================================================
        # 1. 优先检查私聊队列 (非阻塞快速检查)
        # ==========================================================
        if not self._private_queue.empty():
            try:
                message = self._private_queue.get_nowait()
                if debug_log:
                    logger.info(f"私聊消息出队 (优先) | 等待: {time.time() - message.enqueue_time:.2f}s")
                return message
            except asyncio.QueueEmpty:
                pass
        
        # ==========================================================
        # 2. 如果群聊也没有活跃流，进入等待状态
        # ==========================================================
        # 注意：这里需要在锁外检查 active_streams 的大概状态，或者依赖 event
        # 为避免竞争，我们依赖 event。如果 event 未设置，说明可能没消息。
        
        if not self._message_event.is_set():
            # 再次检查私聊 (防止 race condition)
            if not self._private_queue.empty():
                return self._private_queue.get_nowait()

            try:
                await asyncio.wait_for(self._message_event.wait(), timeout=timeout)
                self._message_event.clear() # 清除信号
                
                # 唤醒后，再次优先检查私聊
                if not self._private_queue.empty():
                    return self._private_queue.get_nowait()
            except asyncio.TimeoutError:
                return None
        else:
            # 如果 event 已经是 set 状态，清除它以便下次等待
            # 但要小心，如果只有一条消息且被取走了，clear 是对的。
            # 如果有多条消息，clear 后下一次循环应该通过 is_empty 判断来决定是否 wait
            # 这里的逻辑简化：每次 dequeue 消费一个信号。如果有剩余消息，enqueue/dequeue 逻辑应确保 event 再次 set？
            # 不，标准的 event 模式是：有任务 set，空了 clear。
            # 简单起见，这里 clear，如果 active_streams 还有东西，下面逻辑不应受阻。
            # 下次进来如果 active_streams 非空，不应 wait。
            pass

        # ==========================================================
        # 3. 处理群聊消息 (核心调度逻辑)
        # ==========================================================
        async with self._lock:
            # 双重检查
            if not self._active_group_streams:
                self._message_event.clear()
                return None
                
            target_stream_id: Optional[str] = None
            is_sticky_hit = False
            
            # --- A. 尝试粘性策略 ---
            # 如果上次处理的流还有消息，且未超过连续处理限制 (3次)
            if (self._last_stream_id and 
                self._last_stream_id in self._group_queues and 
                self._group_queues[self._last_stream_id] and
                self._sticky_count < self.STICKY_LIMIT):
                
                target_stream_id = self._last_stream_id
                is_sticky_hit = True
            
            # --- B. 否则，轮询下一个活跃流 ---
            if not target_stream_id:
                # 从调度队列头部取出一个流
                # 注意：active_streams 可能包含已经空的流（如果在其他地方被处理了），需要过滤
                while self._active_group_streams:
                    candidate_id = self._active_group_streams.popleft()
                    
                    # 检查该流是否有效且有消息
                    if candidate_id in self._group_queues and self._group_queues[candidate_id]:
                        target_stream_id = candidate_id
                        
                        # 重置粘性状态，因为切换了流
                        self._last_stream_id = target_stream_id
                        self._sticky_count = 0 
                        break
                    else:
                        # 发现空流，清理掉 (从 group_queues 删除)
                        if candidate_id in self._group_queues:
                            del self._group_queues[candidate_id]
                        # 且不放回 active_streams，直接丢弃
            
            # --- C. 取出消息并维护状态 ---
            if target_stream_id:
                queue = self._group_queues[target_stream_id]
                message = queue.popleft()
                
                # 更新计数
                self._sticky_count += 1
                
                if debug_log:
                    hit_type = f"粘性 ({self._sticky_count})" if is_sticky_hit else "轮询"
                    logger.info(
                        f"群聊消息出队 | {hit_type} | "
                        f"stream: {target_stream_id} | "
                        f"剩余: {len(queue)}"
                    )

                # 状态维护：如果该流还有剩余消息
                if queue:
                    # 如果是通过轮询拿出来的，需要放回活跃列表末尾 (Round Robin)
                    if not is_sticky_hit:
                        self._active_group_streams.append(target_stream_id)
                    # 如果是通过粘性拿出来的，它本身就在活跃列表里 (上次轮询后放回去的)，不需要动
                    
                    # 确保 Event 状态正确：还有消息，保持 Set
                    self._message_event.set()
                    
                else:
                    # 该流空了
                    del self._group_queues[target_stream_id]
                    
                    # 如果是通过粘性拿出来的，它还在 active_streams 里，需要移除
                    if is_sticky_hit:
                        try:
                            self._active_group_streams.remove(target_stream_id)
                        except ValueError:
                            pass
                    # 如果是通过轮询拿出来的，已经 pop 出来了且不放回，自然移除了
                    
                    # 清除粘性记录，防止下次空转
                    self._last_stream_id = None
                    self._sticky_count = 0
                    
                    # 检查是否还有其他流有消息
                    if self._active_group_streams:
                        self._message_event.set()
                    else:
                        self._message_event.clear()

                return message
            
            else:
                # 居然没找到可用的流 (虽然 active_streams 非空)
                # 说明 active_streams 里都是空的脏数据，已被上面循环清理完
                self._message_event.clear()
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
            # 这里的逻辑需要小心，Queue 没有 iterator，只能 pop 再 push
            # 但 asyncio.Queue 是线程安全的 FIFO，我们可以安全地操作
            # 为了遍历，我们把所有取出来检查，未超时的放回去
            # 这是一个 O(N) 操作
            
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
            
            # 将未超时的消息放回队列 (保持原有顺序)
            for msg in temp_messages:
                await self._private_queue.put(msg)
                
        return timeout_messages
        
    def get_stats(self) -> dict:
        """获取队列统计信息"""
        group_total = sum(len(q) for q in self._group_queues.values())
        return {
            "pending_private": self._private_queue.qsize(),
            "pending_group": group_total,
            "total_pending": self._private_queue.qsize() + group_total,
            "active_streams": len(self._group_queues)
        }
        
    def is_empty(self) -> bool:
        """检查队列是否为空"""
        return self._private_queue.empty() and not self._active_group_streams
        
    def get_pending_count(self) -> int:
        """获取待处理消息总数"""
        return self._private_queue.qsize() + sum(len(q) for q in self._group_queues.values())
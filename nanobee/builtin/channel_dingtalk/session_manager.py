"""Per-conversation serial queue for DingTalk message processing.

Ensures messages belonging to the same conversation are processed
one at a time, in arrival order, while different conversations
can proceed in parallel.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from nanobee.utils.logger import logger

class ConversationQueue:
    """Per-conversation serial message queue.

    Each conversation gets its own asyncio queue. Workers pull from the queue
    sequentially, guaranteeing in-order processing for the same conversation.
    Different conversations are processed concurrently.
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[tuple[Any, Callable[..., Awaitable[None]]]]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._running = True

    async def enqueue_message(
        self,
        conversation_id: str,
        message: Any,
        handler: Callable[..., Awaitable[None]],
    ) -> None:
        """Enqueue a message for the given conversation.

        Creates a queue + worker if this is the first message for the
        conversation. The message will be processed in FIFO order.
        """
        if conversation_id not in self._queues:
            self._queues[conversation_id] = asyncio.Queue()
            self._workers[conversation_id] = asyncio.create_task(
                self._worker_loop(conversation_id)
            )

        await self._queues[conversation_id].put((message, handler))

    async def _worker_loop(self, conversation_id: str) -> None:
        """Pull and process messages sequentially for one conversation."""
        queue = self._queues[conversation_id]
        try:
            while self._running:
                try:
                    # Use queue.get() directly - asyncio.wait_for handles
                    # cancellation of the inner coroutine correctly in Python 3.7+
                    message, handler = await asyncio.wait_for(
                        queue.get(), timeout=300.0
                    )
                except asyncio.TimeoutError:
                    # Idle timeout — clean up
                    break
                except asyncio.CancelledError:
                    # 让 CancelledError 传播，不吞掉
                    raise

                try:
                    await handler(message)
                except Exception as e:
                    # Handler exceptions must not break the worker loop,
                    # but we log them for debugging.
                    logger.exception("'[Queue] Handler error: {}'", e)
                finally:
                    queue.task_done()
        finally:
            self._cleanup_conversation(conversation_id)

    def _cleanup_conversation(self, conversation_id: str) -> None:
        """Remove queue and worker references for a conversation."""
        self._queues.pop(conversation_id, None)
        self._workers.pop(conversation_id, None)

    async def shutdown(self) -> None:
        """Cancel all workers and clean up."""
        self._running = False
        tasks = list(self._workers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._queues.clear()
        self._workers.clear()

    @property
    def active_conversations(self) -> int:
        """Number of conversations currently being processed."""
        return len(self._queues)

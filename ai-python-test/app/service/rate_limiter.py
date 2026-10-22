import asyncio
import time
from collections import deque
from typing import Deque


class SlidingWindowRateLimiter:
    """Limitador de ventana deslizante (mismo algoritmo que usa el provider).

    `acquire()` espera hasta que haya hueco en la ventana. El lock se mantiene
    mientras se espera, de modo que los llamantes se atienden en orden FIFO y
    no se produce una estampida cuando se libera un hueco.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._timestamps: Deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self._window:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._limit:
                    self._timestamps.append(now)
                    return
                await asyncio.sleep(self._window - (now - self._timestamps[0]) + 0.01)

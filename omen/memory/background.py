"""Background indexing and re-embedding queue for Omen Memory.

Indexing and re-embedding run in the background, are pausable by the
resource monitor, and never block chat.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class BackgroundIndexer:
    """Manages background vector indexing tasks."""

    def __init__(
        self,
        index_fn: Callable[[str, str], None],
        *,
        monitor: Any = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._index_fn = index_fn
        self._monitor = monitor
        self._on_event = on_event
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._paused = threading.Event()
        self._paused.clear()  # Not paused initially
        self._stop_event = threading.Event()
        self._processed_count = 0
        self._worker_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the background worker thread."""
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._run,
                name="OmenMemoryIndexer",
                daemon=True,
            )
            self._worker_thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop worker thread and wait for completion."""
        self._stop_event.set()
        self._queue.put(None)
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                self._worker_thread.join(timeout=timeout)
                self._worker_thread = None

    def pause(self, reason: str = "resource pressure") -> None:
        """Pause indexing."""
        if not self._paused.is_set():
            self._paused.set()
            if self._on_event:
                self._on_event(
                    {
                        "event_type": "memory_indexer_paused",
                        "reason": reason,
                        "pending_queue_size": self._queue.qsize(),
                    }
                )

    def resume(self) -> None:
        """Resume indexing."""
        if self._paused.is_set():
            self._paused.clear()
            if self._on_event:
                self._on_event(
                    {
                        "event_type": "memory_indexer_resumed",
                        "pending_queue_size": self._queue.qsize(),
                    }
                )

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def enqueue(self, record_id: str, text: str) -> None:
        """Enqueue an item for background vector indexing. Does not block chat."""
        if self._stop_event.is_set():
            return
        self._queue.put((record_id, text))

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until all current items in queue are processed (useful for tests)."""
        deadline = time.time() + timeout
        while not self._queue.empty():
            if time.time() > deadline:
                return False
            time.sleep(0.01)
        # Give current item a moment to finish processing
        time.sleep(0.02)
        return True

    def check_resource_pressure(self) -> None:
        """Check ResourceMonitor if configured and auto-pause under pressure."""
        if self._monitor is None:
            return
        try:
            level = self._monitor.level()
            if level in ("degrade", "emergency"):
                if not self.is_paused():
                    self.pause(f"resource monitor level is {level}")
            elif level in ("normal", "warn"):
                if self.is_paused():
                    self.resume()
        except Exception as exc:
            logger.warning("Resource pressure check failed: %s", exc)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            # Check resource monitor pressure periodically
            self.check_resource_pressure()

            # If paused, wait a bit before checking again
            if self._paused.is_set():
                time.sleep(0.1)
                continue

            try:
                task = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if task is None:  # Shutdown sentinel
                break

            record_id, text = task
            try:
                self._index_fn(record_id, text)
                with self._lock:
                    self._processed_count += 1
            except Exception as exc:
                logger.warning("Error indexing record %s: %s", record_id, exc)
            finally:
                self._queue.task_done()

    def status(self) -> dict[str, Any]:
        return {
            "running": self._worker_thread is not None and self._worker_thread.is_alive(),
            "paused": self.is_paused(),
            "pending_tasks": self._queue.qsize(),
            "processed_count": self._processed_count,
        }

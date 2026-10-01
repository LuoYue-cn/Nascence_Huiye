"""Thread-safe application signals. No GUI imports or implicit worker startup."""
import logging
import threading

class Signal:
    def __init__(self):
        self._handlers = []
        self._lock = threading.RLock()

    def connect(self, handler):
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)

    def disconnect(self, handler):
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def emit(self, *args):
        with self._lock:
            handlers = tuple(self._handlers)
        for handler in handlers:
            try:
                handler(*args)
            except Exception:
                logging.getLogger(__name__).exception("Application event subscriber failed")

class EventBus:
    def __init__(self):
        for name in ("log", "status", "task_error", "message"):
            setattr(self, name, Signal())

BUS = EventBus()

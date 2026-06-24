"""Small shared utilities for worker lenses."""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Optional


class LRUCache:
    """Thread-safe LRU cache.

    Bundles the (OrderedDict, lock, max_size) trio that several lenses use
    to memoize expensive per-text analysis results. One object instead of
    four loose attributes.

    Not generic over key/value type by design — both are `Any`, callers
    pass whatever they want. Hashable keys are the only contract.
    """

    __slots__ = ("_data", "_lock", "max_size")

    def __init__(self, max_size: int):
        if max_size <= 0:
            raise ValueError(f"max_size must be > 0, got {max_size}")
        self._data: "OrderedDict[Any, Any]" = OrderedDict()
        self._lock = threading.Lock()
        self.max_size = max_size

    def get(self, key: Any) -> Optional[Any]:
        """Return value and mark recently-used, or None if absent."""
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
            return None

    def put(self, key: Any, value: Any) -> None:
        """Insert/update; evict oldest entries beyond `max_size`."""
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __contains__(self, key: Any) -> bool:
        with self._lock:
            return key in self._data
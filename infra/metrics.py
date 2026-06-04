import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Metrics:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _counters: dict[str, int] = field(default_factory=dict, repr=False)
    _histograms: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def inc(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value

    def record(self, name: str, value: float) -> None:
        with self._lock:
            self._histograms.setdefault(name, []).append(value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = {k: v for k, v in self._counters.items()}
            for name, values in self._histograms.items():
                sorted_vals = sorted(values)
                n = len(sorted_vals)
                if n:
                    result[f"{name}_p50"] = sorted_vals[int(n * 0.50)]
                    result[f"{name}_p95"] = sorted_vals[int(n * 0.95)]
            return result


metrics = Metrics()

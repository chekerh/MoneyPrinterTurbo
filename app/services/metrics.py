"""
Metrics collection and instrumentation for MoneyPrinterTurbo.

Provides:
- Counter, Gauge, Histogram metrics
- Prometheus-compatible /metrics endpoint
- Built-in instrumentation for key operations
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from contextlib import contextmanager
import functools


@dataclass
class MetricValue:
    value: float
    labels: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class Counter:
    """Monotonically increasing counter."""

    def __init__(self, name: str, description: str = "", labels: dict = None):
        self.name = name
        self.description = description
        self.labels = labels or {}
        self._value = 0.0
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, labels: dict = None):
        with self._lock:
            self._value += amount

    def get(self) -> float:
        with self._lock:
            return self._value


class Gauge:
    """Gauge that can go up and down."""

    def __init__(self, name: str, description: str = "", labels: dict = None):
        self.name = name
        self.description = description
        self.labels = labels or {}
        self._value = 0.0
        self._lock = threading.Lock()

    def set(self, value: float, labels: dict = None):
        with self._lock:
            self._value = value

    def inc(self, amount: float = 1.0, labels: dict = None):
        with self._lock:
            self._value += amount

    def dec(self, amount: float = 1.0, labels: dict = None):
        with self._lock:
            self._value -= amount

    def get(self) -> float:
        with self._lock:
            return self._value


class Histogram:
    """Histogram for tracking distributions (latencies, sizes)."""

    def __init__(
        self,
        name: str,
        description: str = "",
        labels: dict = None,
        buckets: tuple = (
            0.005,
            0.01,
            0.025,
            0.05,
            0.1,
            0.25,
            0.5,
            1.0,
            2.5,
            5.0,
            10.0,
            float("inf"),
        ),
    ):
        self.name = name
        self.description = description
        self.labels = labels or {}
        self.buckets = buckets
        self._counts = [0] * len(buckets)
        self._sum = 0.0
        self._count = 0
        self._lock = threading.Lock()

    def observe(self, value: float, labels: dict = None):
        with self._lock:
            self._count += 1
            self._sum += value
            for i, bucket in enumerate(self.buckets):
                if value <= bucket:
                    self._counts[i] += 1

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "count": self._count,
                "sum": self._sum,
                "buckets": dict(zip([str(b) for b in self.buckets], self._counts)),
            }


class MetricsRegistry:
    """Central registry for all metrics."""

    def __init__(self):
        self._metrics: dict[str, Counter | Gauge | Histogram] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, description: str = "", labels: dict = None) -> Counter:
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = Counter(name, description, labels)
            return self._metrics[name]

    def gauge(self, name: str, description: str = "", labels: dict = None) -> Gauge:
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = Gauge(name, description, labels)
            return self._metrics[name]

    def histogram(
        self, name: str, description: str = "", labels: dict = None, buckets: tuple = None
    ) -> Histogram:
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = Histogram(name, description, labels, buckets)
            return self._metrics[name]

    def get_all(self) -> dict:
        with self._lock:
            return dict(self._metrics)

    def export_prometheus(self) -> str:
        """Export all metrics in Prometheus text format."""
        lines = []
        with self._lock:
            for name, metric in self._metrics.items():
                if isinstance(metric, Counter):
                    lines.append(f"# HELP {name} {metric.description}")
                    lines.append(f"# TYPE {name} counter")
                    labels_str = self._format_labels(metric.labels)
                    lines.append(f"{name}{labels_str} {metric.get()}")
                elif isinstance(metric, Gauge):
                    lines.append(f"# HELP {name} {metric.description}")
                    lines.append(f"# TYPE {name} gauge")
                    labels_str = self._format_labels(metric.labels)
                    lines.append(f"{name}{labels_str} {metric.get()}")
                elif isinstance(metric, Histogram):
                    lines.append(f"# HELP {name} {metric.description}")
                    lines.append(f"# TYPE {name} histogram")
                    stats = metric.get_stats()
                    for bucket, count in stats["buckets"].items():
                        labels_str = self._format_labels(
                            {**metric.labels, "le": bucket}
                        )
                        lines.append(f"{name}_bucket{labels_str} {count}")
                    labels_str = self._format_labels(metric.labels)
                    lines.append(f"{name}_sum{labels_str} {stats['sum']}")
                    lines.append(f"{name}_count{labels_str} {stats['count']}")
        return "\n".join(lines) + "\n"

    def _format_labels(self, labels: dict) -> str:
        if not labels:
            return ""
        pairs = [f'{k}="{v}"' for k, v in sorted(labels.items())]
        return "{" + ",".join(pairs) + "}"


# Global registry
registry = MetricsRegistry()


# Pre-defined metrics for MoneyPrinterTurbo
VIDEO_GENERATION_TOTAL = registry.counter(
    "mpt_video_generation_total",
    "Total number of video generation attempts",
    labels={"status": "unknown"},
)

VIDEO_GENERATION_DURATION = registry.histogram(
    "mpt_video_generation_duration_seconds",
    "Video generation duration in seconds",
    buckets=(
        10,
        30,
        60,
        120,
        300,
        600,
        1200,
        1800,
        3600,
        float("inf"),
    ),
)

VIDEO_GENERATION_STAGE_DURATION = registry.histogram(
    "mpt_video_generation_stage_duration_seconds",
    "Duration of each video generation stage",
    buckets=(
        1,
        5,
        10,
        30,
        60,
        120,
        300,
        600,
        float("inf"),
    ),
)

TASK_QUEUE_SIZE = registry.gauge(
    "mpt_task_queue_size",
    "Number of tasks waiting in queue",
)

TASK_ACTIVE_COUNT = registry.gauge(
    "mpt_task_active_count",
    "Number of currently active (processing) tasks",
)

LLM_REQUEST_TOTAL = registry.counter(
    "mpt_llm_request_total",
    "Total LLM requests",
    labels={"provider": "unknown", "status": "unknown"},
)

LLM_REQUEST_DURATION = registry.histogram(
    "mpt_llm_request_duration_seconds",
    "LLM request duration in seconds",
    buckets=(
        0.5,
        1,
        2,
        5,
        10,
        30,
        60,
        120,
        float("inf"),
    ),
)

MATERIAL_SEARCH_TOTAL = registry.counter(
    "mpt_material_search_total",
    "Total material searches",
    labels={"provider": "unknown", "cache": "unknown"},
)

MATERIAL_SEARCH_DURATION = registry.histogram(
    "mpt_material_search_duration_seconds",
    "Material search duration in seconds",
    buckets=(
        0.1,
        0.5,
        1,
        2,
        5,
        10,
        30,
        float("inf"),
    ),
)

MATERIAL_DOWNLOAD_DURATION = registry.histogram(
    "mpt_material_download_duration_seconds",
    "Material download duration in seconds",
    buckets=(
        1,
        5,
        10,
        30,
        60,
        120,
        300,
        float("inf"),
    ),
)

FFMPEG_ENCODE_DURATION = registry.histogram(
    "mpt_ffmpeg_encode_duration_seconds",
    "FFmpeg encoding duration in seconds",
    buckets=(
        5,
        10,
        30,
        60,
        120,
        300,
        600,
        float("inf"),
    ),
)

TTS_SYNTHESIS_DURATION = registry.histogram(
    "mpt_tts_synthesis_duration_seconds",
    "TTS synthesis duration in seconds",
    buckets=(
        1,
        5,
        10,
        30,
        60,
        120,
        300,
        float("inf"),
    ),
)

CACHE_HIT_TOTAL = registry.counter(
    "mpt_cache_hit_total",
    "Cache hits",
    labels={"cache": "unknown"},
)

CACHE_MISS_TOTAL = registry.counter(
    "mpt_cache_miss_total",
    "Cache misses",
    labels={"cache": "unknown"},
)

ACTIVE_CONNECTIONS = registry.gauge(
    "mpt_active_connections",
    "Active HTTP connections in pool",
)

SYSTEM_CPU_USAGE = registry.gauge(
    "mpt_system_cpu_usage_percent",
    "System CPU usage percentage",
)

SYSTEM_MEMORY_USAGE = registry.gauge(
    "mpt_system_memory_usage_bytes",
    "System memory usage in bytes",
)

PROCESS_CPU_USAGE = registry.gauge(
    "mpt_process_cpu_usage_percent",
    "Process CPU usage percentage",
)

PROCESS_MEMORY_USAGE = registry.gauge(
    "mpt_process_memory_usage_bytes",
    "Process memory usage in bytes",
)


@contextmanager
def track_duration(histogram: Histogram, labels: dict = None):
    """Context manager to track duration of a block."""
    start = time.perf_counter()
    try:
        yield
    finally:
        duration = time.perf_counter() - start
        histogram.observe(duration, labels)


def track_stage(stage_name: str):
    """Decorator to track duration of a pipeline stage."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with track_duration(VIDEO_GENERATION_STAGE_DURATION, {"stage": stage_name}):
                return func(*args, **kwargs)

        return wrapper

    return decorator


def increment_counter(counter: Counter, labels: dict = None, amount: float = 1.0):
    """Increment a counter with labels."""
    counter.inc(amount, labels)


def set_gauge(gauge: Gauge, value: float, labels: dict = None):
    """Set a gauge value with labels."""
    gauge.set(value, labels)


# System metrics collector
class SystemMetricsCollector:
    """Collects system-level metrics periodically."""

    def __init__(self, interval: float = 30.0):
        self.interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _collect_loop(self):
        while self._running:
            try:
                self._collect()
            except Exception as e:
                logger.warning(f"Failed to collect system metrics: {e}")
            time.sleep(self.interval)

    def _collect(self):
        try:
            import psutil

            process = psutil.Process(os.getpid())

            # Process metrics
            PROCESS_CPU_USAGE.set(process.cpu_percent(interval=None))
            PROCESS_MEMORY_USAGE.set(process.memory_info().rss)

            # System metrics
            SYSTEM_CPU_USAGE.set(psutil.cpu_percent(interval=None))
            mem = psutil.virtual_memory()
            SYSTEM_MEMORY_USAGE.set(mem.used)
        except ImportError:
            pass  # psutil not available


# Global collector
_system_collector = SystemMetricsCollector()


def start_metrics_collection(interval: float = 30.0):
    """Start periodic system metrics collection."""
    global _system_collector
    _system_collector = SystemMetricsCollector(interval)
    _system_collector.start()


def stop_metrics_collection():
    """Stop periodic system metrics collection."""
    _system_collector.stop()


# Logger for system metrics collection
logger = logging.getLogger(__name__)
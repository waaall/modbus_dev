"""缓存真实通信时间戳；接收数据与曲线重绘互不决定频率。"""
from collections import deque
import math

from .live_policy import CURVE_MAX_POINTS, CURVE_WINDOW_S


class TrendHistory:
    def __init__(self):
        self.series = {}
        self.last_samples = {}
        self.backend_dropped = 0
        self.capacity_dropped = 0
        self.gap_keys = set()

    @property
    def dropped(self):
        return self.backend_dropped + self.capacity_dropped

    def clear(self):
        self.series.clear()
        self.last_samples.clear()
        self.backend_dropped = self.capacity_dropped = 0
        self.gap_keys.clear()

    def _append(self, key, at, value, now):
        points = self.series.setdefault(key, deque())
        while points and points[0][0] < now - CURVE_WINDOW_S:
            points.popleft()
        if len(points) == CURVE_MAX_POINTS:
            points.popleft()
            self.capacity_dropped += 1
        points.append((at, value))

    def sample(self, snapshot, now):
        dropped = snapshot.get("samples_dropped", 0)
        if dropped > self.backend_dropped:
            self.gap_keys.update(self.series)
            self.backend_dropped = dropped
        samples = snapshot.get("samples")
        if samples is None:
            # 兼容只带最新值的状态包；相同时间戳不会重复生成点。
            samples = [(row.get("sample_id", row["sampled_s"]), row["key"],
                        row["sampled_s"], row.get("trend_value", row["value"]), row["quality"])
                       for row in snapshot.get("rows", [])]
        stale_after = snapshot.get("stale_after_s", 1.)
        for identity, key, at, value, quality in samples:
            if at < now - CURVE_WINDOW_S:
                continue
            valid = quality in ("received", "submitted", "confirmed") and value is not None and math.isfinite(value)
            value = value if valid else None
            previous = self.last_samples.get(key)
            if previous and identity < previous[0]:
                continue
            if previous and identity == previous[0]:
                # ACK 只修改已存在点的质量，不延长其时间或补造采样点。
                points = self.series.get(key, ())
                for index in range(len(points) - 1, -1, -1):
                    if points[index][0] == at:
                        points[index] = (at, value)
                        break
                continue
            if previous and (at - previous[1] > stale_after or key in self.gap_keys):
                points = self.series.get(key)
                if points and points[-1][1] is not None:
                    self._append(key, at, None, now)
            self.gap_keys.discard(key)
            self._append(key, at, value, now)
            self.last_samples[key] = (identity, at)
        self.advance(now, snapshot.get("state", "idle"), stale_after)

    def advance(self, now, state, stale_after=1.):
        for key, points in self.series.items():
            previous = self.last_samples.get(key)
            if points and previous and points[-1][1] is not None:
                if state != "running" or now - previous[1] > stale_after:
                    self._append(key, min(now, previous[1] + stale_after), None, now)
            while points and points[0][0] < now - CURVE_WINDOW_S:
                points.popleft()

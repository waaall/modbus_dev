"""保留原始解码数值并缓存显示量程趋势，不记录发送计划或源计划值。"""
from collections import deque
from itertools import islice
import threading

from ..domain.numeric import decode_value
from ..domain.linear_transform import transform_value
from ..domain.live_policy import CURVE_WINDOW_S, LIVE_BATCH_MAX_SAMPLES, LIVE_SAMPLE_CAPACITY


def decode_float(words, order):
    # 旧诊断入口委托公共解码器，不再维护独立的浮点排列逻辑。
    try:
        return decode_value(words, "float32", order)
    except ValueError:
        return None


class LiveValues:
    def __init__(self, config, *, capacity=LIVE_SAMPLE_CAPACITY):
        if capacity < 1:
            raise ValueError("Live sample capacity must be positive")
        self.config = config
        self.lock = threading.Lock()
        self.rows = {}
        self.samples = deque()
        self.capacity = capacity
        self.dropped = 0
        self.sequence = 0

    def _enqueue(self, row):
        if len(self.samples) == self.capacity:
            self.samples.popleft()
            self.dropped += 1
        self.samples.append([row["sample_id"], row["key"], row["sampled_s"], row["trend_value"], row["quality"]])

    def consume(self, event):
        if event.code not in ("register_values", "register_confirmation"):
            return
        data = event.details
        start = data["address"]
        words = tuple(data.get("registers", ()))
        stop = start + (len(words) or data.get("count", 0))
        with self.lock:
            for index, point in enumerate(self.config.points):
                if (not point.enabled or self.config.point_area(point) != data["area"]
                        or stop <= point.address or start >= point.address + point.register_count):
                    continue
                key = f"{data['area']}:{index}:{data['direction']}"
                if event.code == "register_confirmation":
                    row = self.rows.get(key)
                    if row is not None and data.get("sampled_s", row["sampled_s"]) == row["sampled_s"]:
                        # ACK describes this write attempt, not the submitted value's validity.
                        # Updating a row must not enqueue duplicate points or refresh its age.
                        row["confirmation"] = data["quality"]
                    continue
                point_stop = point.address + point.register_count
                complete = start <= point.address and stop >= point_stop
                raw = words[max(start, point.address) - start:min(stop, point_stop) - start]
                value = None
                if complete:
                    try:
                        value = decode_value(raw, point.data_type, point.encoding_order())
                    except ValueError:
                        pass
                quality = data.get("quality", "received") if complete else "partial"
                if complete and value is None:
                    quality = "nonfinite"
                mapped = transform_value(value, point.linear_transform)
                self.sequence += 1
                self.rows[key] = dict(key=key, sample_id=self.sequence, point=index, name=point.name or f"测点 {index + 1}",
                                     area=data["area"], address=point.address, direction=data["direction"],
                                     data_type=point.data_type, byte_order=point.encoding_order(),
                                     value=value, mapped_value=mapped.value, mapping_status=mapped.status,
                                     mapping_out_of_range=mapped.out_of_range,
                                     trend_value=mapped.value if point.linear_transform.enabled else value,
                                     registers=list(raw), function=data["function"],
                                     quality=quality, confirmation=None,
                                     sampled_s=data["sampled_s"])
                self._enqueue(self.rows[key])

    def snapshot(self, now, state, *, drain=False):
        with self.lock:
            while self.samples and self.samples[0][2] < now - CURVE_WINDOW_S:
                self.samples.popleft()
                self.dropped += 1
            samples = list(islice(self.samples, LIVE_BATCH_MAX_SAMPLES))
            if drain:
                for _ in samples:
                    self.samples.popleft()
            return dict(sampled_s=now, state=state,
                        stale_after_s=max(1., 3 * self.config.period_ms / 1000),
                        rows=[dict(row, registers=list(row["registers"])) for row in self.rows.values()],
                        samples=samples, samples_dropped=self.dropped,
                        samples_pending=len(self.samples) if drain else max(0, len(self.samples) - len(samples)))

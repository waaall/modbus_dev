"""统一计算线路耗时、每周期流程预留、硬下限与黄色预警线。"""
from dataclasses import dataclass
import math

from .config import MIN_PERIOD_MS

WORKFLOW_RESERVE_MS = 10
WARNING_MULTIPLIER = 1.2


@dataclass(frozen=True)
class CycleBudget:
    transactions: int
    wire_bytes: int
    transaction_ms: float
    line_ms: float
    minimum_period_ms: int
    warning_period_ms: int
    reserve_ms: int = WORKFLOW_RESERVE_MS

    def level(self, period_ms):
        if period_ms < self.minimum_period_ms:
            return "error"
        return "warning" if period_ms < self.warning_period_ms else "idle"


def cycle_budget(config):
    from .operations import cycle_requests
    # slot=0 包含所有启用的写后读回，为最忙周期预留，不按平均周期摊薄。
    requests = cycle_requests(config, [0] * len(config.enabled_points), slot=0)
    total = sum((9 + 2 * request.count if request.function == 16 else 8) +
                (8 if request.function in (6, 16) else 5 + 2 * request.count) for request in requests)
    transactions = len(requests)
    character_ms = 1000 * config.serial.bits_per_character / config.serial.baud
    transaction_ms = total * character_ms + max(0, 2 * transactions - 1) * config.serial.gap_ms
    line_ms = transaction_ms + (config.serial.gap_ms if transactions else 0)
    minimum = max(MIN_PERIOD_MS, math.ceil(round(line_ms + WORKFLOW_RESERVE_MS, 9)))
    return CycleBudget(transactions, total, transaction_ms, line_ms, minimum,
                       math.ceil(round(minimum * WARNING_MULTIPLIER, 9)))


def minimum_cycle_ms(config):
    """兼容既有诊断字段：返回不含周期末静默与流程预留的交易线速预算。"""
    return cycle_budget(config).transaction_ms

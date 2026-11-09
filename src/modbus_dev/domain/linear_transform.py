"""通用线性量程换算，仅产生显示结果，不修改原始数值或通信状态。"""
from dataclasses import dataclass
from decimal import Decimal, localcontext
import math


@dataclass(frozen=True)
class TransformResult:
    value: float | None
    status: str
    out_of_range: bool = False


def transform_value(value, config):
    """对有效解码数值外推；换算失败只标记显示结果，不向通信层抛错。"""
    if not config.enabled:
        return TransformResult(None, "disabled")
    if type(value) not in (int, float) or not math.isfinite(value):
        return TransformResult(None, "invalid")
    outside = value < config.raw_start or value > config.raw_end
    try:
        # 高精度中间量避免有限端点相减或相乘时提前溢出；最终仍输出有限浮点数。
        with localcontext() as context:
            context.prec = 50
            start, end, target_start, target_end = (
                Decimal(str(getattr(config, key)))
                for key in ("raw_start", "raw_end", "mapped_start", "mapped_end"))
            mapped = float(target_start + (Decimal(str(value)) - start)
                           * (target_end - target_start) / (end - start))
        if not math.isfinite(mapped):
            return TransformResult(None, "overflow", outside)
    except (ArithmeticError, ValueError):
        return TransformResult(None, "overflow", outside)
    return TransformResult(mapped, "ok", outside)

"""与协议库无关的请求和应答契约；单笔执行未知与应答关联风险分开记录。"""
from dataclasses import dataclass


def valid_reply_counts(counters):
    """Accepted frames count independently of their request-correlation certainty."""
    return dict(valid_ack=counters.get("ack", 0) + counters.get("ack_ambiguous", 0),
                valid_read=counters.get("read", 0) + counters.get("read_ambiguous", 0))


@dataclass(frozen=True)
class Request:
    function: int
    address: int
    count: int
    registers: tuple[int, ...] = ()


@dataclass(frozen=True)
class Reply:
    status: str
    registers: tuple[int, ...] = ()
    sent_s: float | None = None
    received_s: float | None = None
    ambiguous: bool = False
    detail: str = ""
    exception_code: int | None = None
    outcome_unknown: bool = False


class StopRequested(Exception):
    """Cooperative stop; never a protocol failure."""

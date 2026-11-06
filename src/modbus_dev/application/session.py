"""CLI 与 GUI 共用的单会话入口，按启用测点执行独立采样与分组通信。"""
from collections import Counter
from pathlib import Path
import threading
import traceback

from ..domain.events import Event
from ..domain.operations import cycle_requests
from ..domain.transactions import StopRequested, valid_reply_counts
from ..sources.synthetic import SYNTHETIC_KINDS, SyntheticSource
from ..reporting.journal import Journal, LogWriteError
from .control import Control
from .metrics import Metric
from .timing import AbsoluteSchedule, SCHEDULE_LATE_BASIS
from .live import LiveValues

_ACTIVE_SESSION = threading.Lock()


def check_capabilities(config):
    config.validate()
    if config.usage.source and any(point.source.kind not in SYNTHETIC_KINDS for point in config.enabled_points if config.point_usage(point).source):
        raise ValueError("This build runs constant/sine/triangle sources only; offline sources are deferred until after GUI")


class SessionRunner:
    def __init__(self, config, directory, *, stop=None, control=None,
                 engine_factory=None, journal_factory=Journal):
        check_capabilities(config)
        self.config, self.directory = config, Path(directory)
        self.control = control or Control(stop)
        self.engine_factory, self.journal_factory = engine_factory, journal_factory
        self.state = "idle"
        self._used = False
        self.counters = Counter()
        self.metrics = {name: Metric() for name in ("send_interval_ms", "send_lateness_ms", "response_ms",
                                                   "cycle_interval_ms", "cycle_lateness_ms")}
        self.previous_send = None
        self.previous_cycle_send = None
        self.last_sent_slot = None
        self.journal = self.engine = None
        self.current_slot = None
        self.current_scheduled = None
        self.current_request = None
        self.live = LiveValues(config)

    def _emit(self, code, message, level="INFO", phase="runtime", **details):
        self.journal.emit(Event(code, message, level, phase, details))

    def _state(self, state):
        if state != self.state:
            previous, self.state = self.state, state
            self._emit("state_changed", f"{previous} -> {state}", previous=previous, state=state)

    def _health(self):
        if self.journal.failed.is_set():
            raise LogWriteError(self.journal.snapshot()["log_error"] or "Recording failed")

    def _endpoint(self):
        """诊断信息按传输返回真实端点，不用串口名称伪装 TCP 地址。"""
        if self.config.usage.serial:
            return dict(port=self.config.serial.port)
        return dict(host=self.config.host, port=self.config.port)

    def _connect(self):
        """共用连接策略；引擎只负责一次有界连接，不进行隐式重试。"""
        serial = self.config.usage.serial
        try:
            self.engine.connect()
            self.counters["port_opens" if serial else "connection_opens"] += 1
            self._state("running")
            return True
        except (OSError, ValueError) as exc:
            self.engine.close()
            self.counters["open_errors"] += 1
            self._emit("port_open_failed" if serial else "tcp_connect_failed", str(exc), "ERROR", "connect",
                       **self._endpoint(), traceback=traceback.format_exc(), retry_s=1)
            if self.config.policy == "strict":
                raise
            self._state("reconnecting")
            return False

    def _engine_event(self, event):
        details = dict(event.details)
        details.update(slot=self.current_slot, **self._endpoint(), unit=self.config.unit)
        if event.code == "write_outcome_unknown":
            self.counters["write_outcome_unknown"] += 1
            self.live.consume(Event("register_confirmation", event.message, "DEBUG", "communication",
                                    dict(area="holding", direction="tx", address=details["address"],
                                         count=details["count"], sampled_s=details["sent_s"], quality="outcome-unknown")))
        if event.code == "tx":
            request = self.current_request
            packet = details.get("hex", "")
            if request and request.function in (6, 16) and details.get("bytes_written") == len(packet) // 2:
                self._value_event(request, request.registers, "tx", details["sent_s"], "submitted")
            sent = details["sent_s"]
            self.counters["write_attempts"] += 1
            if self.previous_send is not None:
                self.metrics["send_interval_ms"].add((sent - self.previous_send) * 1000)
            self.previous_send = sent
            if self.current_scheduled is not None:
                offset_ms = (sent - self.current_scheduled) * 1000
                self.metrics["send_lateness_ms"].add(offset_ms)
                details.update(scheduled_s=self.current_scheduled, schedule_offset_ms=offset_ms)
            first_send = self.current_slot != self.last_sent_slot
            details["cycle_first_send"] = first_send
            if first_send:
                interval_ms = None
                if self.previous_cycle_send is not None:
                    interval_ms = (sent - self.previous_cycle_send) * 1000
                    self.metrics["cycle_interval_ms"].add(interval_ms)
                    # Inclusive upper bound, with 1 ns slack for floating point noise.
                    limit_s = self.config.cycle_interval_limit_ms / 1000
                    if sent > self.previous_cycle_send + limit_s + 1e-9:
                        self.counters["schedule_late"] += 1
                        self._emit("schedule_late", "Interval between cycle first sends exceeded tolerance",
                                   "WARNING", "scheduling", basis=SCHEDULE_LATE_BASIS,
                                   slot=self.current_slot, previous_slot=self.last_sent_slot,
                                   previous_sent_s=self.previous_cycle_send, sent_s=sent,
                                   interval_ms=interval_ms, period_ms=self.config.period_ms,
                                   interval_excess_ms=interval_ms - self.config.period_ms,
                                   interval_limit_ms=self.config.cycle_interval_limit_ms,
                                   tolerance_ms=self.config.schedule_tolerance_ms,
                                   tolerance_percent=self.config.schedule_tolerance_percent)
                details.update(cycle_interval_ms=interval_ms,
                               previous_cycle_sent_s=self.previous_cycle_send,
                               previous_cycle_slot=self.last_sent_slot)
                self.metrics["cycle_lateness_ms"].add(offset_ms)
                self.previous_cycle_send, self.last_sent_slot = sent, self.current_slot
        self.journal.emit(Event(event.code, event.message, event.level, event.phase, details))

    def _timing_summary(self):
        interval = self.metrics["cycle_interval_ms"]
        mean_ms = interval.mean if interval.count else None
        return dict(schedule_late_basis=SCHEDULE_LATE_BASIS,
                    period_ms=self.config.period_ms,
                    tolerance_ms=self.config.schedule_tolerance_ms,
                    cycle_interval_limit_ms=self.config.cycle_interval_limit_ms,
                    cycle_first_sends=self.metrics["cycle_lateness_ms"].count,
                    cycle_intervals=interval.count,
                    mean_cycle_interval_ms=mean_ms,
                    actual_cycle_rate_hz=1000 / mean_ms if mean_ms is not None and mean_ms > 0 else None)

    def _transaction(self, request, deadline, slot, scheduled):
        self.current_request = request
        try:
            reply = self.engine.exchange(request, deadline)
        finally:
            self.current_request = None
        if reply.status == "read":
            quality = "ambiguous" if reply.ambiguous else "received"
            if request.registers and reply.registers != request.registers:
                quality = "readback-mismatch"
            self._value_event(request, reply.registers, "rx", reply.received_s,
                              quality)
        elif request.function in (6, 16) and reply.sent_s is not None:
            quality = "outcome-unknown" if reply.outcome_unknown else reply.status
            if reply.status == "ack":
                quality = "ambiguous" if reply.ambiguous else "confirmed"
            event = Event("register_confirmation", "写入应答状态", "DEBUG", "communication",
                          dict(area="holding", direction="tx", address=request.address, count=request.count,
                               sampled_s=reply.sent_s, quality=quality))
            self.live.consume(event)
        status = reply.status + ("_ambiguous" if reply.ambiguous and reply.status in ("ack", "read", "exception") else "")
        self.counters[status] += 1
        if reply.status == "connection_lost":
            self.counters["io_errors"] += 1
        if reply.received_s is not None and reply.sent_s is not None:
            self.metrics["response_ms"].add((reply.received_s - reply.sent_s) * 1000)
        # A sticky correlation flag is historical evidence, not a fresh frame failure.
        # Keep every accepted reply in the journal without flooding the GUI preview.
        if reply.status in ("ack", "read"):
            level = "DEBUG" if reply.ambiguous else "INFO"
        else:
            level = "WARNING"
        self._emit("transaction", status, level, "communication", slot=slot, status=status,
                   **self._endpoint(), unit=self.config.unit, function=request.function,
                   address=request.address, count=request.count, sent_s=reply.sent_s,
                   received_s=reply.received_s, deadline_s=deadline, timeout_ms=self.config.timeout_ms,
                   ambiguous=reply.ambiguous, exception_code=reply.exception_code,
                   outcome_unknown=reply.outcome_unknown, detail=reply.detail)
        return reply

    def _value_event(self, request, registers, direction, sampled_s, quality):
        event = Event("register_values", "实际寄存器数值", "DEBUG", "communication",
                      dict(area="input" if request.function == 4 else "holding", direction=direction,
                           address=request.address, registers=registers, function=request.function,
                           sampled_s=self.control.now() if sampled_s is None else sampled_s, quality=quality))
        self.live.consume(event)
        self.journal.emit(event)

    def run(self):
        if self.config.mode in ("tcp-slave", "rtu-slave"):
            from .slave import run_slave
            return run_slave(self)
        if self._used or not _ACTIVE_SESSION.acquire(blocking=False):
            raise RuntimeError("Only one session can run in this process; use a new runner after completion")
        self._used = True
        result = None
        old_health = self.control.health_check
        try:
            # Resolve dependencies before creating output or acquiring hardware.
            if self.engine_factory is None:
                if self.config.mode == "tcp-master":
                    from ..engines.tcp_master import TcpMasterEngine
                    self.engine_factory = TcpMasterEngine
                else:
                    from ..engines.rtu import RtuEngine
                    self.engine_factory = RtuEngine
            from ..engines.platform import WindowsTimer
            usage = self.config.usage
            source = SyntheticSource(self.config) if usage.source else None
            self.journal = self.journal_factory(self.directory, self.config.to_mapping())
            self.control.health_check = self._health
            started = self.control.now()
            reason, error = "duration_complete", None
            try:
                self._state("starting")
                self.engine = self.engine_factory(self.config, self.control, self._engine_event)
                with WindowsTimer() as timer:
                    self._emit("session_started", f"{self.config.mode} starting", timer=timer.status,
                               engine="pymodbus-codec/pyserial-deadline" if usage.serial else "pymodbus-codec/tcp-deadline",
                               mode=self.config.mode, **self._endpoint(), policy=self.config.policy,
                               schedule_late_basis=SCHEDULE_LATE_BASIS,
                               cycle_interval_limit_ms=self.config.cycle_interval_limit_ms,
                               schedule_tolerance_percent=self.config.schedule_tolerance_percent,
                               schedule_tolerance_ms=self.config.schedule_tolerance_ms)
                    self._connect()
                    reconnect_at = self.control.now() + 1
                    # 首次连接尝试后建立统一的绝对周期，连接失败也不暂停源时钟。
                    origin = self.control.now() + .1
                    schedule = AbsoluteSchedule(origin, self.config.period_ms / 1000,
                                                self.config.timeout_ms / 1000, self.config.policy)
                    slot = 0
                    while not self.config.duration_s or slot * schedule.period < self.config.duration_s:
                        decision = schedule.at(slot, self.control.now())
                        if decision.skipped:
                            self.counters["skipped_slots"] += decision.skipped
                            self._emit("schedule_skip", "Skipped expired slots; no catch-up burst", "WARNING",
                                       first_slot=slot, next_slot=decision.slot)
                            slot = decision.slot
                            continue
                        self.control.wait_until(decision.scheduled_s)
                        # Scheduling may overrun while sleeping or checking recording.
                        if self.control.now() - decision.scheduled_s >= schedule.period:
                            continue
                        # 预先确定整轮计划；传输可在此释放需要轮换的连接，但不能暗中建连。
                        values = source.sample(slot) if source is not None else ()
                        requests = cycle_requests(self.config, values, slot=slot)
                        self.engine.prepare_cycle(requests)
                        if not self.engine.connected:
                            if self.control.now() < reconnect_at:
                                self.counters["port_unavailable" if usage.serial else "connection_unavailable"] += 1
                                slot += 1
                                continue
                            if not self._connect():
                                reconnect_at = self.control.now() + 1
                                slot += 1
                                continue
                        self.control.check()
                        self.current_slot, self.current_scheduled = slot, decision.scheduled_s
                        # Reserve the complete remaining group plan before the first write.
                        minimum = sum(self.engine.minimum_s(r) for r in requests)
                        minimum += (len(requests) - 1) * self.engine.request_gap_s
                        if self.control.now() + minimum >= decision.deadline_s:
                            self.counters["schedule_missed"] += 1
                            self._emit("schedule_missed", "Insufficient budget for the complete cycle", "WARNING", slot=slot)
                            if self.config.policy == "strict":
                                raise RuntimeError("Strict replay has insufficient cycle budget")
                            slot += 1
                            continue
                        if source is not None:
                            self._emit("source_sample", "Source values prepared", "DEBUG", "source", slot=slot, values=values)
                        try:
                            # 明确拒绝的写入不再读回验证；独立请求仍共享原来的截止预算。
                            rejected_writes = []
                            for request in requests:
                                if request.function == 3 and request.registers and any(
                                        request.address < end and start < request.address + request.count
                                        for start, end in rejected_writes):
                                    self.counters["readback_skipped"] += 1
                                    self._emit("readback_skipped", "Readback depends on a rejected write", "WARNING",
                                               "verification", slot=slot, address=request.address, count=request.count)
                                    continue
                                reply = self._transaction(request, decision.deadline_s, slot, decision.scheduled_s)
                                if reply.status == "read" and request.registers and reply.registers != request.registers:
                                    self.counters["readback_mismatch"] += 1
                                    self._emit("readback_mismatch", "Readback differs from source registers", "ERROR",
                                               "verification", slot=slot, address=request.address,
                                               expected=request.registers, actual=reply.registers)
                                    if self.config.policy == "strict":
                                        raise RuntimeError("Readback differs from source registers")
                                    if reply.ambiguous:
                                        break
                                if reply.status not in ("ack", "read") or reply.ambiguous:
                                    if self.config.policy == "strict":
                                        raise RuntimeError(f"Strict replay transaction failed: {reply.status}; ambiguous={reply.ambiguous}")
                                # 完整且关联明确的异常应答已结束交易，不等于超时或交易未决。
                                if reply.status == "exception" and not reply.ambiguous and not reply.outcome_unknown:
                                    if request.function in (6, 16):
                                        rejected_writes.append((request.address, request.address + request.count))
                                elif reply.status not in ("ack", "read"):
                                    break
                                if not self.engine.connected:
                                    break
                        except (OSError, ValueError) as exc:
                            self.counters["io_errors"] += 1
                            if usage.serial:
                                self.engine.mark_uncertain("Serial I/O failed; request or reply may remain in flight")
                            self._emit("serial_io_error" if usage.serial else "tcp_io_error", str(exc), "ERROR", "communication",
                                       slot=slot, **self._endpoint(), traceback=traceback.format_exc())
                            self.engine.close()
                            if self.config.policy == "strict":
                                raise
                        # TCP 超时/残帧由引擎关闭连接，RTU 仍保持原有的超时关联语义。
                        if not self.engine.connected:
                            reconnect_at = self.control.now() + 1
                            self._state("reconnecting")
                        slot += 1
                    self.control.check()
            except (StopRequested, KeyboardInterrupt):
                reason = "user_stopped"
            except Exception as exc:
                reason, error = "failed", f"{type(exc).__name__}: {exc}"
                self._emit(getattr(exc, "code", "session_failed"), str(exc), "ERROR",
                           traceback=traceback.format_exc())
            finally:
                self._state("stopping")
                if self.engine is not None:
                    try:
                        self.engine.close()
                    except Exception as exc:
                        reason, error = "failed", f"Close failed: {exc}"
                        self._emit("port_close_failed" if usage.serial else "tcp_close_failed", str(exc), "ERROR")
                self._state("failed" if error else "stopped")
                result = dict(state=self.state, stop_reason=reason, error=error,
                              exit_code=1 if error else 130 if reason == "user_stopped" else 0,
                              elapsed_s=self.control.now() - started, counters=dict(self.counters),
                              ack_correlation_uncertain=bool(self.engine and self.engine.uncertain),
                              reply_summary=valid_reply_counts(self.counters),
                              timing=self._timing_summary(),
                              metrics={name: metric.report() for name, metric in self.metrics.items()})
                try:
                    self.journal.close(run_summary=result)
                except (OSError, LogWriteError) as exc:
                    self.state = "failed"
                    result.update(state="failed", stop_reason="log_write_failed", error=str(exc), exit_code=1)
            return result
        finally:
            self.control.health_check = old_health
            _ACTIVE_SESSION.release()

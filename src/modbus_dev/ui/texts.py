"""将稳定的协议键映射为界面文案，供配置与监视组件共用。"""
from .i18n import tr
from ..domain.config import MIN_PERIOD_MS, MAX_PERIOD_MS
CHOICE_LABELS = {
    "tcp-slave": tr('TCP slave'), "tcp-master": tr('TCP master'), "rtu-slave": tr('RTU slave'), "rtu-master": tr('RTU master'),
    "continuous": tr('Continuous'), "strict": tr('Strict'), "constant": tr('Constant'), "sine": tr('Sine wave'),
    "triangle": tr('Triangle wave'),
    "read-only": tr('Read-only'), "read-write": tr('Read/write'),
    "read-holding": tr('FC03 · Read holding registers'), "read-input": tr('FC04 · Read input registers'),
    "write-single": tr('FC06 · Write single registers'), "write-multiple": tr('FC16 · Write multiple registers'),
}
COUNTER_LABELS = {
    "source_updates": tr('Data updates'), "slave_request": tr('Slave requests'), "rx": tr('Receive calls'), "tx": tr('Transmit calls'),
    "listener_opens": tr('Listener opens'), "port_opens": tr('Port opens'), "open_errors": tr('Open failures'),
    "io_errors": tr('I/O errors'), "write_attempts": tr('Requests sent'), "ack": tr('Regular ACKs'), "read": tr('Regular read replies'),
    "valid_ack": tr('Valid ACKs'), "valid_read": tr('Valid read replies'),
    "ack_ambiguous": tr('Uncertain ACK correlations'), "read_ambiguous": tr('Uncertain read correlations'),
    "exception": tr('Protocol exceptions'), "exception_ambiguous": tr('Uncertain exception correlations'), "timeout": tr('Reply timeout'),
    "schedule_late": tr('Late cycles'), "schedule_missed": tr('Missed transaction budgets'), "skipped_slots": tr('Skipped cycles'),
    "port_unavailable": tr('Port unavailable'), "readback_mismatch": tr('Readback mismatch'),
    "connection_opens": tr('TCP connections'), "connection_unavailable": tr('TCP connection unavailable'),
    "connection_lost": tr('TCP connection lost'), "write_outcome_unknown": tr('Write outcome unknown'),
}
SCHEDULE_LATE_HELP = (tr('Count adjacent cycle-first send intervals only when they exceed the period plus tolerance; equality and the first send are excluded. For a 100 ms period with 10% tolerance, count intervals above 110 ms. Groups and readbacks within one cycle are not counted again. Planned offsets remain in the detailed logs and session summary.'))
VALID_REPLY_HELP = tr('Count replies accepted within the transaction budget after frame and request-field checks, including uncertain correlations. This does not establish unique per-request correlation or device source quality.')
METRIC_HELP = {"schedule_late": SCHEDULE_LATE_HELP, "valid_ack": VALID_REPLY_HELP, "valid_read": VALID_REPLY_HELP}

# 配置页与逐点编辑共用用途说明，先说明数据由谁生成、由谁读写。
HOLDING_AREA_HELP = tr('Holding: stores values for the master to read with FC03. With read/write access enabled, accepts and retains master writes via FC06/16.')
INPUT_AREA_HELP = tr('Input: the local source generates and updates values for the master to read with FC04. The master cannot write them.')
SLAVE_AREAS_HELP = (HOLDING_AREA_HELP + '\n' + INPUT_AREA_HELP + '\n'
                   + tr('Each point belongs to one register area. The two areas are independent, even at the same address.'))
HOLDING_INITIAL_HELP = tr('Set only when the session starts. Later master writes are retained; the input register update period does not refresh this value.')
INPUT_PERIOD_HELP = tr('{minimum}–{maximum} ms. All enabled input-register points share this local update period. It does not update holding registers or control how often the master reads.').format(minimum=MIN_PERIOD_MS, maximum=MAX_PERIOD_MS)
MASTER_PERIOD_HELP = tr('{minimum}–{maximum} ms. Executes one round of configured point reads and writes per cycle. The minimum depends on the actual requests and includes one 10 ms workflow reserve per cycle.').format(minimum=MIN_PERIOD_MS, maximum=MAX_PERIOD_MS)
TCP_MASTER_PERIOD_HELP = tr('{minimum}–{maximum} ms. All points share one polling cycle and one transaction budget. Network and device latency are not predicted from serial settings; expired cycles are not replayed.').format(minimum=MIN_PERIOD_MS, maximum=MAX_PERIOD_MS)

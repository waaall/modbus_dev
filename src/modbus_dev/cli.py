"""统一配置、串口枚举、通信和发布包诊断入口；不主动加载 Qt。"""
import argparse
import json
import sys
import signal
import threading

from . import __version__
from .domain.config import load_config
from .domain.events import ConfigurationError
from .domain.wire import minimum_cycle_ms, cycle_budget


def main(argv=None):
    parser = argparse.ArgumentParser(prog="modbus-dev", description="Modbus Dev communication tools")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate task configuration without opening a device")
    validate.add_argument("config")
    run = commands.add_parser("run", help="run one RTU/TCP master or slave session (constant/sine/triangle)")
    run.add_argument("config")
    run.add_argument("--output", required=True, help="new result directory; existing directories are refused")
    commands.add_parser("ports", help="list serial ports without opening them")
    commands.add_parser("gui", help="open the optional desktop workbench")
    check = commands.add_parser("self-check", help="inspect native modules in a frozen release")
    check.add_argument("--output", required=True)
    worker = commands.add_parser("worker", help="GUI worker (versioned JSONL control on stdin/stdout)")
    worker.add_argument("config")
    worker.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "self-check":
            from .selfcheck import native_check
            return native_check(args.output)
        if args.command == "gui":
            from .runtime import adjacent_executable, frozen
            if frozen():
                # 独立 GUI 引导器负责初始化 Qt；不能让 CLI/worker 的启动强制加载 QtCore。
                import subprocess
                return subprocess.run([str(adjacent_executable("gui_executable"))]).returncode
            try:
                from .desktop import main as gui_main
            except ImportError as error:
                raise ImportError('GUI requires PySide6: pip install -e ".[gui]"') from error
            return gui_main([])
        if args.command == "worker":
            from .worker import main as worker_main
            return worker_main(args.config, args.output)
        if args.command == "ports":
            from serial.tools.list_ports import comports
            print(json.dumps([dict(port=p.device, description=p.description, hwid=p.hwid) for p in comports()],
                             ensure_ascii=False))
            return 0
        config = load_config(args.config)
        if args.command == "run":
            from .application.session import SessionRunner
            stop = threading.Event()
            previous = {}
            runner = SessionRunner(config, args.output, stop=stop)
            try:
                if threading.current_thread() is threading.main_thread():
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        previous[sig] = signal.signal(sig, lambda *_: stop.set())
                result = runner.run()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            print(json.dumps(result, ensure_ascii=False))
            return result["exit_code"]
        result = dict(valid=True, schema_version=config.schema_version, mode=config.mode,
                      period_ms=config.period_ms, configuration_only=True)
        if config.mode == "tcp-master":
            from .application.timing import SCHEDULE_LATE_BASIS
            from .domain.operations import cycle_requests
            result.update(host=config.host, port=config.port, connect_timeout_ms=config.connect_timeout_ms,
                          timeout_ms=config.timeout_ms, timeout_basis="whole_cycle",
                          request_count=len(cycle_requests(config, slot=0)),
                          schedule_tolerance_percent=config.schedule_tolerance_percent,
                          schedule_tolerance_ms=config.schedule_tolerance_ms,
                          schedule_late_basis=SCHEDULE_LATE_BASIS,
                          cycle_interval_limit_ms=config.cycle_interval_limit_ms)
        if config.mode == "rtu-master":
            from .application.timing import SCHEDULE_LATE_BASIS
            budget = cycle_budget(config)
            minimum = budget.transaction_ms
            result.update(schedule_tolerance_percent=config.schedule_tolerance_percent,
                          schedule_tolerance_ms=config.schedule_tolerance_ms,
                          schedule_late_basis=SCHEDULE_LATE_BASIS,
                          cycle_interval_limit_ms=config.cycle_interval_limit_ms,
                          minimum_cycle_ms=minimum, remaining_processing_budget_ms=config.timeout_ms - minimum,
                          gap_ms=config.serial.gap_ms, request_count=budget.transactions,
                          line_ms=budget.line_ms, workflow_reserve_ms=budget.reserve_ms,
                          minimum_period_ms=budget.minimum_period_ms,
                          warning_period_ms=budget.warning_period_ms,
                          budget_status=budget.level(config.period_ms))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ConfigurationError, OSError, ValueError, ImportError) as error:
        print(json.dumps(dict(valid=False, code=getattr(error, "code", "configuration_read_failed"),
                              message=str(error)), ensure_ascii=False), file=sys.stderr)
        return 2

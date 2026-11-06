"""验证四种角色统一周期下限，文件和进程入口不得绕过校验。"""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from modbus_dev.domain.config import MIN_PERIOD_MS, TaskConfig, load_config
from modbus_dev.domain.events import ConfigurationError
from modbus_dev.domain.wire import cycle_budget


MODES = ('tcp-master', 'tcp-slave', 'rtu-master', 'rtu-slave')


def configuration(mode, period):
    return dict(schema_version=4, mode=mode, period_ms=period, timeout_ms=40,
                serial=dict(port='TEST', baud=115200))


class PeriodLimitTests(unittest.TestCase):
    def test_all_roles_reject_lower_periods_and_accept_the_boundary(self):
        self.assertEqual(MIN_PERIOD_MS, 50)
        for mode in MODES:
            config = TaskConfig.from_mapping(configuration(mode, MIN_PERIOD_MS))
            for period in (20, 40, MIN_PERIOD_MS - 1):
                with self.subTest(mode=mode, period=period):
                    with self.assertRaises(ConfigurationError):
                        TaskConfig.from_mapping(configuration(mode, period))
                    with self.assertRaises(ConfigurationError):
                        replace(config, period_ms=period).validate()

    def test_file_and_cli_worker_reject_before_opening_devices_or_results(self):
        # 无效配置必须在打开 TEST 串口或监听默认端口之前被拒绝。
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for mode in MODES:
                path = root / (mode + '.json')
                path.write_text(json.dumps(configuration(mode, MIN_PERIOD_MS - 1)))
                with self.subTest(mode=mode), self.assertRaises(ConfigurationError):
                    load_config(path)
                for command in ('validate', 'run', 'worker'):
                    output = root / (mode + '-' + command)
                    args = [sys.executable, '-m', 'modbus_dev', command, str(path)]
                    if command != 'validate':
                        args += ['--output', str(output)]
                    completed = subprocess.run(args, input='', text=True, capture_output=True, timeout=5)
                    with self.subTest(mode=mode, command=command):
                        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
                        self.assertFalse(output.exists())

    def test_rtu_wire_budget_can_still_require_more_than_the_common_floor(self):
        config = TaskConfig.from_mapping(configuration('rtu-master', 50))
        slow = replace(config, serial=replace(config.serial, baud=1200))
        budget = cycle_budget(slow)
        self.assertGreater(budget.minimum_period_ms, MIN_PERIOD_MS)
        with self.assertRaises(ConfigurationError):
            slow.validate()
        # 周期与交易预算都按慢速线路调整后仍可启动。
        replace(slow, period_ms=budget.minimum_period_ms,
                timeout_ms=int(budget.transaction_ms) + 1).validate()

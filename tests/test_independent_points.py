"""验证八点独立配置、混合操作、区域隔离及周期预算边界。"""
from dataclasses import replace
import json
import struct
import unittest

from modbus_dev.application.control import Control
from modbus_dev.application.live import LiveValues
from modbus_dev.domain.config import MAX_POINTS, PointConfig, TaskConfig
from modbus_dev.domain.events import ConfigurationError, Event
from modbus_dev.domain.operations import cycle_requests
from modbus_dev.domain.wire import cycle_budget
from modbus_dev.engines.register_banks import RegisterBanks
from modbus_dev.sources.synthetic import SyntheticSource


def master(points, **options):
    return TaskConfig.from_mapping(dict(schema_version=4, mode='rtu-master',
        serial=dict(port='TEST', baud=115200), points=points, **options))


def slave(points, **options):
    return TaskConfig.from_mapping(dict(schema_version=4, mode='tcp-slave', points=points, **options))


class IndependentPointTests(unittest.TestCase):
    def test_eight_points_have_explicit_orders_and_no_global_settings_on_export(self):
        config = slave([dict(address=2*i, source=dict(value=i+.25)) for i in range(MAX_POINTS)])
        self.assertEqual(SyntheticSource(config).sample(0), tuple(i+.25 for i in range(MAX_POINTS)))
        mapping = config.to_mapping()
        self.assertEqual(mapping['schema_version'], 4)
        for obsolete in ('byte_order', 'master_operation', 'readback_every', 'read_function'):
            self.assertNotIn(obsolete, mapping)
        self.assertTrue(all(point['byte_order'] == 'B1234' for point in mapping['points']))
        self.assertEqual(TaskConfig.from_mapping(json.loads(json.dumps(mapping))), config)
        with self.assertRaises(ConfigurationError):
            slave([dict(address=2*i) for i in range(MAX_POINTS+1)])

    def test_16_bit_defaults_to_12_and_32_bit_defaults_to_1234(self):
        config = slave([dict(data_type='uint16'), dict(address=2, data_type='float32')])
        self.assertEqual([point.byte_order for point in config.enabled_points], ['B12', 'B1234'])

    def test_overlaps_are_scoped_to_register_area(self):
        config = slave([dict(area='holding', data_type='uint16'), dict(area='input', data_type='float32')])
        self.assertEqual(len(config.enabled_points), 2)
        with self.assertRaises(ConfigurationError):
            slave([dict(area='input'), dict(area='input', address=1)])
        master([dict(operation='read-holding'), dict(operation='read-input')])
        with self.assertRaises(ConfigurationError):
            master([dict(operation='read-holding'), dict(operation='write-multiple')])

    def test_mixed_plan_merges_only_contiguous_points_of_the_same_operation(self):
        config = master([dict(operation='read-input', address=0), dict(operation='read-input', address=2),
                         dict(operation='read-holding', address=0),
                         dict(operation='write-multiple', address=10, readback_every=2, source=dict(value=1.25)),
                         dict(operation='write-single', address=20, data_type='uint16', source=dict(value=7))])
        values = SyntheticSource(config).sample(0)
        self.assertEqual(values, (None, None, None, 1.25, 7))
        self.assertEqual([(r.function, r.address, r.count) for r in cycle_requests(config, values, slot=0)],
                         [(3, 0, 2), (4, 0, 4), (6, 20, 1), (16, 10, 2), (3, 10, 2)])
        self.assertEqual([(r.function, r.address) for r in cycle_requests(config, values, slot=1)],
                         [(3, 0), (4, 0), (6, 20), (16, 10)])

    def test_readback_frequencies_keep_independent_boundaries(self):
        config = master([dict(operation='write-multiple', address=0, readback_every=2),
                         dict(operation='write-multiple', address=2, readback_every=3)])
        self.assertEqual([r.address for r in cycle_requests(config, slot=2) if r.function == 3], [0])
        self.assertEqual([r.address for r in cycle_requests(config, slot=3) if r.function == 3], [2])
        self.assertEqual([r.address for r in cycle_requests(config, slot=6) if r.function == 3], [0, 2])
        self.assertEqual(cycle_budget(config).transactions, 4)

    def test_fc06_rejects_32_bit_but_sends_16_bit_points_separately(self):
        with self.assertRaises(ConfigurationError) as failure:
            master([dict(operation='write-single', data_type='float32')])
        self.assertEqual(failure.exception.reason, 'fc06_width')
        config = master([dict(operation='write-single', data_type='uint16', address=i) for i in range(3)])
        self.assertEqual([r.count for r in cycle_requests(config)], [1, 1, 1])

    def test_only_write_sources_are_validated_and_sampled(self):
        config = master([dict(operation='read-input', source=dict(kind='future', value='unused')),
                         dict(operation='write-multiple', address=10, source=dict(value=2.5))])
        self.assertEqual(SyntheticSource(config).sample(0), (None, 2.5))





    def test_disabled_unknown_drafts_remain_inert_and_roundtrip(self):
        config = slave([dict(address=0), dict(enabled=False, data_type='future', byte_order='unused',
                                              source=dict(kind='future', value='unfinished'))])
        self.assertEqual(TaskConfig.from_mapping(json.loads(json.dumps(config.to_mapping()))), config)

    def test_process_reserve_is_once_per_cycle_and_warning_is_120_percent(self):
        for spacing, requests, minimum in ((2, 1, 444), (4, 8, 1610)):
            config = master([dict(address=i*spacing, operation='read-input') for i in range(8)],
                            period_ms=3000, timeout_ms=2500)
            config = replace(config, serial=replace(config.serial, baud=1200))
            budget = cycle_budget(config)
            self.assertEqual(budget.transactions, requests)
            self.assertEqual(budget.minimum_period_ms, minimum)
            self.assertAlmostEqual(budget.minimum_period_ms - budget.line_ms, 10, delta=1)
            self.assertEqual(budget.level(minimum-1), 'error')
            self.assertEqual(budget.level(minimum), 'warning')
            self.assertEqual(budget.level(budget.warning_period_ms-1), 'warning')
            self.assertEqual(budget.level(budget.warning_period_ms), 'idle')

    def test_period_floor_and_exact_single_point_boundaries(self):
        for baud, minimum, warning in ((1200, 210, 252), (9600, 50, 60), (115200, 50, 60)):
            config = master([dict(operation='read-input')], period_ms=1000, timeout_ms=900)
            config = replace(config, serial=replace(config.serial, baud=baud))
            budget = cycle_budget(config)
            self.assertEqual((budget.minimum_period_ms, budget.warning_period_ms), (minimum, warning))
            with self.assertRaises(ConfigurationError):
                replace(config, period_ms=minimum-1).validate()
        for period in (20, 40, 49, 60001):
            with self.assertRaises(ConfigurationError):
                slave([{}], period_ms=period)
        slave([{}], period_ms=50)
        slave([{}], period_ms=60000)
        # 流程预留属于整周期，不在每笔请求或内部预算中重复计入。
        config = master([dict(operation='read-input')], period_ms=50, timeout_ms=30)
        config = replace(config, serial=replace(config.serial, baud=9600))
        config.validate()

    def test_budget_counts_readbacks_on_the_busiest_cycle(self):
        plain = master([dict(operation='write-multiple')])
        readback = master([dict(operation='write-multiple', readback_every=100)])
        self.assertEqual(cycle_budget(plain).transactions, 1)
        self.assertEqual(cycle_budget(readback).transactions, 2)
        self.assertGreater(cycle_budget(readback).transaction_ms, cycle_budget(plain).transaction_ms)
        self.assertEqual(cycle_budget(readback).minimum_period_ms, 50)

    def test_bank_publication_does_not_create_unconfigured_mirrors(self):
        config = slave([dict(area='input')])
        bank = RegisterBanks(config, Control(), lambda event: None)
        bank.publish({'input': {0: 0x3f80, 1: 0}, 'holding': {}})
        self.assertEqual(bank.response(1, struct.pack('>BHH', 4, 0, 2)), b'\x04\x04\x3f\x80\x00\x00')
        self.assertEqual(bank.response(1, struct.pack('>BHH', 3, 0, 2)), b'\x83\x02')

    def test_both_transports_share_isolated_banks_and_retain_master_writes(self):
        for mode in ('tcp-slave', 'rtu-slave'):
            with self.subTest(mode=mode):
                config = TaskConfig.from_mapping(dict(schema_version=4, mode=mode, access_mode='read-write',
                    serial=dict(port='TEST'), points=[dict(area='holding'), dict(area='input')]))
                bank = RegisterBanks(config, Control(), lambda event: None)
                bank.publish({'holding': {0: 0, 1: 0}, 'input': {0: 0x4000, 1: 0}})
                bank.response(1, struct.pack('>BHHBHH', 16, 0, 2, 4, 0x3fa0, 0))
                bank.publish({'holding': {0: 0, 1: 0}, 'input': {0: 0x4040, 1: 0}})
                self.assertEqual(bank.response(1, struct.pack('>BHH', 3, 0, 2)), b'\x03\x04\x3f\xa0\x00\x00')
                self.assertEqual(bank.response(1, struct.pack('>BHH', 4, 0, 2)), b'\x04\x04\x40\x40\x00\x00')

    def test_live_values_never_decode_the_same_address_in_the_other_area(self):
        config = slave([dict(area='holding', data_type='uint16'), dict(area='input')])
        live = LiveValues(config)
        live.consume(Event('register_values', 'value', details=dict(area='input', direction='tx', address=0,
            function=4, registers=[0x3fa0, 0], quality='submitted', sampled_s=0)))
        rows = live.snapshot(0, 'running')['rows']
        self.assertEqual([(row['point'], row['value']) for row in rows], [(1, 1.25)])

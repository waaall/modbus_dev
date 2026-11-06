"""核对主站连续模式的请求组隔离、依赖读回和严格模式边界。"""
from pathlib import Path
import struct
import tempfile
import unittest

from modbus_dev.application.session import SessionRunner
from modbus_dev.domain.config import TaskConfig
from modbus_dev.engines.rtu import RtuEngine
from modbus_dev.engines.tcp_master import TcpMasterEngine
from test_rtu_master_boundaries import ResponsePeer
from test_rtu_session import FakeControl, frame
from test_tcp_master import BytePeer, Clock, adu


class MasterFaultIsolationTests(unittest.TestCase):
    def exercise(self, transport, points, failure='exception', policy='continuous'):
        """独立字节对端只让首地址失败，其余请求始终正常应答。"""
        clock = Clock() if transport == 'tcp' else FakeControl()
        observed = []

        def respond(packet):
            pdu = packet[7:] if transport == 'tcp' else packet[1:-2]
            function, address, count = struct.unpack('>BHH', pdu[:5])
            observed.append((function, address))
            if address == 0 and failure == 'timeout':
                return []
            if address == 0 and failure in ('exception', 'ambiguous-exception'):
                response = bytes((function | 0x80, 2))
            elif function in (6, 16):
                response = pdu[:5]
            else:
                value = 99 if address == 0 and failure == 'mismatch' else 7
                response = bytes((function, count * 2)) + struct.pack('>' + 'H' * count, *([value] * count))
            encoded = (adu(int.from_bytes(packet[:2], 'big'), response)
                       if transport == 'tcp' else frame(bytes((packet[0],)) + response))
            return [(.002, encoded)]

        peer = BytePeer(clock, respond) if transport == 'tcp' else ResponsePeer(clock, respond)
        def factory(config, control, emit):
            if transport == 'tcp':
                return TcpMasterEngine(config, control, emit, socket_factory=lambda *_: peer, waiter=peer.wait)
            engine = RtuEngine(config, control, emit, lambda _: peer)
            engine.uncertain = failure == 'ambiguous-exception'
            return engine
        config = TaskConfig.from_mapping(dict(mode=transport + '-master', policy=policy,
            access_mode='read-write', duration_s=.3, serial=dict(port='TEST', baud=115200), points=points))
        with tempfile.TemporaryDirectory() as root:
            runner = SessionRunner(config, Path(root) / 'run', control=clock, engine_factory=factory)
            result = runner.run()
        return result, observed

    def test_explicit_read_exception_does_not_starve_independent_point(self):
        points = [dict(operation='read-input', data_type='uint16', address=address) for address in (0, 4)]
        for transport in ('tcp', 'rtu'):
            with self.subTest(transport=transport):
                result, observed = self.exercise(transport, points)
                self.assertEqual(result['exit_code'], 0)
                self.assertEqual(observed, [(4, 0), (4, 4)] * 3)
                self.assertEqual(result['counters']['exception'], 3)
                self.assertEqual(result['counters']['read'], 3)

    def test_rejected_write_skips_dependent_readback_but_keeps_other_groups(self):
        for operation in ('write-single', 'write-multiple'):
            points = [dict(operation=operation, data_type='uint16', address=address,
                           readback_every=1, source=dict(value=7)) for address in (0, 4)]
            function = 6 if operation == 'write-single' else 16
            for transport in ('tcp', 'rtu'):
                with self.subTest(transport=transport, operation=operation):
                    result, observed = self.exercise(transport, points)
                    self.assertEqual(observed, [(function, 0), (function, 4), (3, 4)] * 3)
                    self.assertEqual(result['counters']['readback_skipped'], 3)
                    self.assertNotIn('readback_mismatch', result['counters'])

    def test_readback_mismatch_does_not_block_later_frequency_group(self):
        points = [dict(operation='write-multiple', data_type='uint16', address=address,
                       readback_every=frequency, source=dict(value=7))
                  for address, frequency in ((0, 1), (4, 2))]
        for transport in ('tcp', 'rtu'):
            with self.subTest(transport=transport):
                result, observed = self.exercise(transport, points, 'mismatch')
                self.assertEqual(result['counters']['readback_mismatch'], 3)
                self.assertEqual(observed.count((16, 4)), 3)
                self.assertEqual(observed.count((3, 4)), 2)

    def test_strict_exception_and_timeout_still_stop_the_round(self):
        points = [dict(operation='read-input', data_type='uint16', address=address) for address in (0, 4)]
        for transport in ('tcp', 'rtu'):
            for failure, policy in (('exception', 'strict'), ('timeout', 'continuous')):
                with self.subTest(transport=transport, failure=failure):
                    result, observed = self.exercise(transport, points, failure, policy)
                    self.assertNotIn((4, 4), observed)
                    if policy == 'strict':
                        self.assertEqual(result['exit_code'], 1)
                        self.assertEqual(len(observed), 1)

    def test_ambiguous_rtu_exception_remains_unresolved(self):
        points = [dict(operation='read-input', data_type='uint16', address=address) for address in (0, 4)]
        result, observed = self.exercise('rtu', points, 'ambiguous-exception')
        self.assertNotIn((4, 4), observed)
        self.assertEqual(result['counters']['exception_ambiguous'], 3)

    def test_fc06_merged_readback_is_skipped_if_any_dependency_was_rejected(self):
        points = [dict(operation='write-single', data_type='uint16', address=address,
                       readback_every=1, source=dict(value=7)) for address in (0, 1, 4)]
        for transport in ('tcp', 'rtu'):
            with self.subTest(transport=transport):
                result, observed = self.exercise(transport, points)
                self.assertEqual(observed, [(6, 0), (6, 1), (6, 4), (3, 4)] * 3)
                self.assertEqual(result['counters']['readback_skipped'], 3)

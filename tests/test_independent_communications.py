"""用实际 RTU 编码器和本地 TCP 字节对端验证八点混合操作及两区独立。"""
from dataclasses import replace
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest

from modbus_dev.application.session import SessionRunner
from modbus_dev.domain.config import TaskConfig
from modbus_dev.engines.rtu import RtuEngine
from modbus_dev.engines.tcp_slave import TcpSlaveEngine
from test_rtu_session import FakeControl, Peer, frame as rtu_frame
from test_tcp_slave import frame, receive


class SingleWordPeer(Peer):
    def write(self, data):
        if data[1] != 6:
            return super().write(data)
        self.writes.append((self.clock.now(), data))
        if rtu_frame(data[:-2]) != data:
            raise AssertionError('Invalid request CRC')
        address, value = struct.unpack('>HH', data[2:6])
        self.registers[address] = value
        self.pending.append((self.clock.now() + self.delay, rtu_frame(data[:6])))
        return len(data)


class IndependentCommunicationTests(unittest.TestCase):
    def test_eight_point_mixed_rtu_cycle_preserves_values_and_independent_readbacks(self):
        points = [dict(operation='read-input', address=0), dict(operation='read-input', address=2),
                  dict(operation='read-holding', address=0, data_type='uint16'),
                  dict(operation='write-single', address=10, data_type='uint16', readback_every=1, source=dict(value=7)),
                  dict(operation='write-multiple', address=20, readback_every=1, source=dict(value=1.25)),
                  dict(operation='read-input', address=30, data_type='int16'),
                  dict(operation='write-multiple', address=40, data_type='int32', readback_every=2, source=dict(value=100)),
                  dict(operation='read-holding', address=50, data_type='uint16')]
        config = TaskConfig.from_mapping(dict(schema_version=4, mode='rtu-master', points=points,
            period_ms=200, timeout_ms=180, duration_s=.4, serial=dict(port='TEST', baud=115200)))
        clock = FakeControl()
        peer = SingleWordPeer(clock, delay=.005)
        with tempfile.TemporaryDirectory() as root:
            runner = SessionRunner(config, Path(root) / 'run', control=clock,
                engine_factory=lambda c, t, e: RtuEngine(c, t, e, lambda _: peer))
            result = runner.run()
            self.assertEqual(result['exit_code'], 0, result)
            self.assertEqual(result['counters'].get('readback_mismatch', 0), 0)
            functions = [packet[1] for _, packet in peer.writes]
            self.assertTrue({3, 4, 6, 16}.issubset(functions))
            self.assertEqual(peer.registers[10], 7)
            self.assertEqual((peer.registers[20], peer.registers[21]), (0x3fa0, 0))
            rows = runner.live.snapshot(clock.now(), 'stopped')['rows']
            self.assertEqual(next(row['value'] for row in rows if row['point'] == 3 and row['direction'] == 'rx'), 7)
            self.assertEqual(next(row['value'] for row in rows if row['point'] == 4 and row['direction'] == 'rx'), 1.25)

    def test_native_tcp_holding_and_input_have_different_widths_and_values_at_the_same_address(self):
        config = TaskConfig.from_mapping(dict(schema_version=4, mode='tcp-slave', duration_s=0,
            access_mode='read-write', period_ms=50, points=[dict(area='holding', data_type='uint16', source=dict(value=7)),
                                                        dict(area='input', source=dict(value=8.5))]))
        ready, stop = threading.Event(), threading.Event()
        engines, results = [], []
        def factory(c, control, emit):
            engine = TcpSlaveEngine(replace(c, port=0), control, emit)
            engines.append(engine)
            publish = engine.publish
            def signal(banks):
                publish(banks)
                ready.set()
            engine.publish = signal
            return engine
        with tempfile.TemporaryDirectory() as root:
            runner = SessionRunner(config, Path(root)/'run', stop=stop, engine_factory=factory)
            thread = threading.Thread(target=lambda: results.append(runner.run()))
            thread.start()
            try:
                self.assertTrue(ready.wait(3), results)
                with socket.create_connection(engines[0].bound_address, timeout=1) as client:
                    for request, expected in ((bytes.fromhex('0300000001'), bytes.fromhex('03020007')),
                        (bytes.fromhex('0400000002'), bytes.fromhex('040441080000')),
                        (bytes.fromhex('0600001234'), bytes.fromhex('0600001234'))):
                        client.sendall(frame(request))
                        self.assertEqual(receive(client), frame(expected))
                    time.sleep(.06)
                    client.sendall(frame(bytes.fromhex('0300000001')))
                    self.assertEqual(receive(client), frame(bytes.fromhex('03021234')))
                    client.sendall(frame(bytes.fromhex('0400000002')))
                    self.assertEqual(receive(client), frame(bytes.fromhex('040441080000')))
            finally:
                stop.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0]['exit_code'], 130)
            rows = runner.live.snapshot(time.perf_counter(), 'stopped')['rows']
            self.assertTrue(any(row['point'] == 0 and row['direction'] == 'rx' and row['value'] == 0x1234 for row in rows))
            self.assertTrue(any(row['point'] == 1 and row['direction'] == 'tx' and row['value'] == 8.5 for row in rows))

import asyncio
import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import tempfile
import unittest

from ledup import encode_payload, reserve_counters


class LedupTests(unittest.TestCase):
    def test_native_reference_bytes(self):
        fixtures = json.loads(Path(__file__).with_name('fixtures').joinpath('native_vectors.json').read_text())
        for case in fixtures['cases']:
            with self.subTest(command=case['command'], packet=case['packet_id']):
                self.assertEqual(encode_payload(bytes.fromhex(case['command']), case['packet_id']).hex(), case['payload'])

    def test_encoding_rejects_invalid_command_or_packet(self):
        for command in (b'', b'123', b'12345', '1234', bytearray(b'1234'), None):
            with self.subTest(command=command), self.assertRaises(ValueError):
                encode_payload(command, 1)
        for packet in (0, -1, 256, 1.5, '1', True, None):
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                encode_payload(b'1234', packet)

    def test_counters_persist_isolate_keys_and_wrap(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'nested' / 'counters.sqlite3'
            self.assertEqual(reserve_counters(state, 'wtrtd200f', 5), (1, 0))
            self.assertEqual(reserve_counters(state, 'wtrtd200f', 5), (2, 1))
            self.assertEqual(reserve_counters(state, 'wtrtd200f', 16), (3, 0))
            self.assertEqual(reserve_counters(state, 'ctr018f', 5), (4, 0))
            with sqlite3.connect(state) as db:
                db.execute("UPDATE counters SET value=255 WHERE name='packet'")
                db.execute("UPDATE counters SET value=65535 WHERE name='click:wtrtd200f:5'")
            self.assertEqual(reserve_counters(state, 'wtrtd200f', 5), (255, 65535))
            self.assertEqual(reserve_counters(state, 'wtrtd200f', 5), (1, 0))

    def test_concurrent_reservations_do_not_reuse_counters(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'counters.sqlite3'
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: reserve_counters(state, 'wtrtd200f', 5), range(24)))
            self.assertEqual(sorted(results), [(packet, packet - 1) for packet in range(1, 25)])

    def test_malformed_state_fails_without_reset_or_partial_reservation(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'counters.sqlite3'
            reserve_counters(state, 'wtrtd200f', 5)
            for name, invalid in [('packet', 0), ('packet', 256), ('packet', 'bad'),
                                  ('click:wtrtd200f:5', -1), ('click:wtrtd200f:5', 65536),
                                  ('click:wtrtd200f:5', 1.5)]:
                with self.subTest(name=name, invalid=invalid):
                    with sqlite3.connect(state) as db:
                        db.execute("UPDATE counters SET value=2 WHERE name='packet'")
                        db.execute("UPDATE counters SET value=1 WHERE name='click:wtrtd200f:5'")
                        db.execute('UPDATE counters SET value=? WHERE name=?', (invalid, name))
                        before = db.execute('SELECT name,value FROM counters ORDER BY name').fetchall()
                    with self.assertRaises(ValueError):
                        reserve_counters(state, 'wtrtd200f', 5)
                    with sqlite3.connect(state) as db:
                        self.assertEqual(db.execute('SELECT name,value FROM counters ORDER BY name').fetchall(), before)
            broken = Path(temp) / 'broken.sqlite3'
            broken.write_bytes(b'not a SQLite database')
            with self.assertRaises(sqlite3.DatabaseError):
                reserve_counters(broken, 'wtrtd200f', 5)
            self.assertEqual(broken.read_bytes(), b'not a SQLite database')


class AdvertisingErrorTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_auth_failure(self, response):
        async def reject(reader, writer):
            try:
                await reader.readuntil(b'\r\n')
                writer.write(response)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        with tempfile.TemporaryDirectory() as temp:
            address = Path(temp) / 'bus'
            server = await asyncio.start_unix_server(reject, path=address)
            async with server:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, str(Path(__file__).resolve().parents[1] / 'ledup.py'),
                    'send', '--product', 'wtrtd200f', '--key', '5',
                    '--state', str(Path(temp) / 'state.sqlite3'),
                    env=dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=f'unix:path={address}'),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                try:
                    out, err = await asyncio.wait_for(process.communicate(), 5)
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.communicate()
                self.assertEqual(process.returncode, 1)
                self.assertEqual(out, b'')
                self.assertNotIn(b'Traceback', err)

    async def test_authentication_rejection_has_no_traceback_or_success(self):
        await self._assert_auth_failure(b'REJECTED EXTERNAL\r\n')

    async def test_authentication_eof_exits_without_traceback_or_success(self):
        await self._assert_auth_failure(b'REJECTED EXTERN')

    @unittest.skipUnless(shutil.which('dbus-daemon'), 'requires a local dbus-daemon, not Bluetooth')
    async def test_optional_properties_and_sigterm_cleanup_with_another_advertiser(self):
        from dbus_next import Message, Variant
        from dbus_next.aio import MessageBus
        from dbus_next.constants import PropertyAccess
        from dbus_next.service import ServiceInterface, dbus_property, method

        class Adapter(ServiceInterface):
            def __init__(self):
                super().__init__('org.bluez.Adapter1')

            @dbus_property(access=PropertyAccess.READ)
            def Powered(self) -> 'b':
                return True

        class Manager(ServiceInterface):
            def __init__(self):
                super().__init__('org.bluez.LEAdvertisingManager1')
                self.unregistering = asyncio.Event()
                self.finish = asyncio.Event()
                self.finished = asyncio.Event()
                self.client = None

            # A two-slot controller with one occupied slot still has room.
            @dbus_property(access=PropertyAccess.READ)
            def SupportedInstances(self) -> 'y':
                return 1

            @dbus_property(access=PropertyAccess.READ)
            def ActiveInstances(self) -> 'y':
                return 1

            @method()
            async def RegisterAdvertisement(self, path: 'o', options: 'a{sv}'):
                self.path = path
                self.txpower = await service.call(Message(
                    destination=self.client, path=path, interface='org.freedesktop.DBus.Properties',
                    member='Get', signature='ss', body=['org.bluez.LEAdvertisement1', 'TxPower'],
                ))
                self.properties = await service.call(Message(
                    destination=self.client, path=path, interface='org.freedesktop.DBus.Properties',
                    member='GetAll', signature='s', body=['org.bluez.LEAdvertisement1'],
                ))
                self.read_only = await service.call(Message(
                    destination=self.client, path=path, interface='org.freedesktop.DBus.Properties',
                    member='Set', signature='ssv',
                    body=['org.bluez.LEAdvertisement1', 'Type', Variant('s', 'broadcast')],
                ))

            @method()
            async def UnregisterAdvertisement(self, path: 'o'):
                if path != self.path:
                    raise AssertionError('unregistering the wrong advertisement')
                self.unregistering.set()
                await self.finish.wait()
                self.finished.set()

        daemon = await asyncio.create_subprocess_exec(
            'dbus-daemon', '--session', '--nofork', '--print-address=1',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        service = None
        process = None
        manager = Manager()
        try:
            address = (await asyncio.wait_for(daemon.stdout.readline(), 5)).decode().strip()
            service = await MessageBus(bus_address=address).connect()
            await service.request_name('org.bluez')
            service.export('/', ServiceInterface('org.ledup.Regression'))
            service.export('/org/bluez/hci0', Adapter())
            service.export('/org/bluez/hci0', manager)

            def capture_client(message):
                if message.interface == 'org.bluez.LEAdvertisingManager1' and message.member == 'RegisterAdvertisement':
                    manager.client = message.sender

            service.add_message_handler(capture_client)
            with tempfile.TemporaryDirectory() as temp:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, str(Path(__file__).resolve().parents[1] / 'ledup.py'),
                    'send', '--product', 'wtrtd200f', '--key', '5', '--duration', '0.01',
                    '--state', str(Path(temp) / 'state.sqlite3'),
                    env=dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=address),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(manager.unregistering.wait(), 5)
                self.assertEqual(manager.txpower.error_name, 'org.freedesktop.DBus.Error.UnknownProperty')
                self.assertNotIn('TxPower', manager.properties.body[0])
                self.assertEqual(manager.properties.body[0]['Type'].value, 'peripheral')
                self.assertEqual(manager.read_only.error_name, 'org.freedesktop.DBus.Error.PropertyReadOnly')
                process.send_signal(signal.SIGTERM)
                out, err = await asyncio.wait_for(process.communicate(), 5)
                owner = await service.call(Message(
                    destination='org.freedesktop.DBus', path='/org/freedesktop/DBus',
                    interface='org.freedesktop.DBus', member='NameHasOwner',
                    signature='s', body=[manager.client],
                ))
                self.assertEqual(process.returncode, 130)
                self.assertEqual(out, b'')
                self.assertNotIn(b'Traceback', err)
                self.assertEqual(owner.body, [False], 'advertising client connection was left alive')
                manager.finish.set()
                await asyncio.wait_for(manager.finished.wait(), 5)
        finally:
            manager.finish.set()
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            if service is not None:
                service.disconnect()
                with contextlib.suppress(EOFError, OSError):
                    await service.wait_for_disconnect()
                # dbus-next 0.2.3 shuts down, but does not close its socket.
                service._stream.close()
                service._sock.close()
            daemon.terminate()
            await daemon.wait()


if __name__ == '__main__':
    unittest.main()

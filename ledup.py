"""Local LEDUP BLE advertising controller (no pairing or GATT connection)."""
import argparse
import asyncio
import contextlib
import json
import math
import os
import signal
import sqlite3
import sys
from pathlib import Path

_BIT_REVERSE = bytes(int(f'{value:08b}'[::-1], 2) for value in range(256))

_BLUEZ_ERRORS = {
    'org.freedesktop.DBus.Error.ServiceUnknown': 'BlueZ service org.bluez is missing; use a Bluetooth-equipped Linux host with bluetoothd running',
    'org.freedesktop.DBus.Error.NameHasNoOwner': 'BlueZ service stopped; start bluetoothd on the Bluetooth host',
    'org.freedesktop.DBus.Error.AccessDenied': 'D-Bus denied Bluetooth access; grant this user permission through the host system-bus policy',
    'org.bluez.Error.NotAuthorized': 'BlueZ denied advertising access; check host Bluetooth permissions',
    'org.bluez.Error.NotPermitted': 'BlueZ cannot allocate an advertising instance; check controller power and other advertising clients',
    'org.bluez.Error.NotSupported': 'This Bluetooth controller/BlueZ stack does not support the requested advertising',
    'org.bluez.Error.InvalidLength': 'BlueZ rejected the advertising data length',
    'org.bluez.Error.InvalidArguments': 'BlueZ rejected the advertisement properties; check the installed BlueZ version',
}


def _whiten(data: bytes, seed: int) -> bytes:
    state = [1] + [(seed >> shift) & 1 for shift in range(5, -1, -1)]
    result = bytearray(data)
    for index in range(len(result)):
        for bit in range(8):
            s0, s1, s2, s3, s4, s5, s6 = state
            result[index] ^= s6 << bit
            state = [s6, s0, s1, s2, s3 ^ s6, s4, s5]
    return bytes(result)


def encode_payload(command: bytes, packet_id: int) -> bytes:
    """Return 16 manufacturer bytes, matching LEDUP 3.7's native encoder."""
    if not isinstance(command, bytes) or len(command) != 4:
        raise ValueError('command must be exactly four bytes')
    if type(packet_id) is not int or not 1 <= packet_id <= 255:
        raise ValueError('packet_id must be an integer in 1..255')
    address = b'\x5a\x00\x10'
    body = b'\xff\xff\xff' + bytes([packet_id]) + command
    crc = 0xffff
    for value in address[::-1] + body.translate(_BIT_REVERSE):
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xffff
    crc = ((_BIT_REVERSE[crc & 255] << 8) | _BIT_REVERSE[crc >> 8]) ^ 0xffff
    prefix = b'\x71\x0f\x55'.translate(_BIT_REVERSE)
    tail = address[::-1].translate(_BIT_REVERSE) + body + crc.to_bytes(2, 'little')
    # The discarded prefix still consumes 120 bits of the outer whitening stream.
    return _whiten(bytes(15) + prefix + _whiten(tail, 0x3f), 0x25)[15:]


def reserve_counters(state: Path, product: str, key: int) -> tuple[int, int]:
    """Atomically reserve counters before RF; a failed send still consumes them."""
    state.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(state)) as connection:
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL)')
            click_name = f'click:{product}:{key}'
            values = {}
            for name, low, high, default in (('packet', 1, 255, 1), (click_name, 0, 65535, 0)):
                row = connection.execute('SELECT value FROM counters WHERE name = ?', (name,)).fetchone()
                value = default if row is None else row[0]
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(f'Corrupt counter {name!r} in {state}: expected {low}..{high}, got {value!r}')
                values[name] = value
                connection.execute(
                    'INSERT INTO counters(name, value) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                    (name, low if value == high else value + 1),
                )
    return values['packet'], values[click_name]


async def advertise(payload: bytes, *, adapter: str | None, duration: float) -> None:
    """Advertise once through BlueZ; success means transmission, not light ACK."""
    if not isinstance(payload, bytes) or len(payload) != 16:
        raise ValueError('payload must be exactly 16 bytes')
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('duration must be finite and positive')
    # Lazy imports keep encoding, catalog listing and dry runs independent of D-Bus.
    from dbus_next import BusType, DBusError, Variant
    from dbus_next.aio import MessageBus
    from dbus_next.constants import PropertyAccess
    from dbus_next.errors import AuthError
    from dbus_next.service import ServiceInterface, dbus_property, method

    class SystemMessageBus(MessageBus):
        # dbus-next 0.2.3's authentication reader spins on EOF.
        async def _auth_readline(self):
            line = b''
            while line[-2:] != b'\r\n':
                chunk = await self._loop.sock_recv(self._sock, 2)
                if not chunk:
                    raise EOFError('System D-Bus disconnected during authentication')
                line += chunk
            return line[:-2].decode()

        def _default_properties_handler(self, message, send_reply):  # ty: ignore[invalid-method-override]
            # dbus-next 0.2.3 re-raises property errors after replying, logging a traceback.
            try:
                super()._default_properties_handler(message, send_reply)
            except DBusError as exc:
                send_reply.send_error(exc)

    released = asyncio.Event()

    class Advertisement(ServiceInterface):
        def __init__(self):
            super().__init__('org.bluez.LEAdvertisement1')

        @dbus_property(access=PropertyAccess.READ)
        def Type(self) -> 's':  # ty: ignore[unresolved-reference]
            return 'peripheral'

        @dbus_property(access=PropertyAccess.READ)
        def ManufacturerData(self) -> 'a{qv}':  # ty: ignore[invalid-syntax-in-forward-annotation]
            return {0xfff0: Variant('ay', payload)}

        @dbus_property(access=PropertyAccess.READ)
        def MinInterval(self) -> 'u':  # ty: ignore[unresolved-reference]
            return 100

        @dbus_property(access=PropertyAccess.READ)
        def MaxInterval(self) -> 'u':  # ty: ignore[unresolved-reference]
            return 100

        @method()
        def Release(self):
            released.set()

    bus = None
    manager = None
    registered = False
    pending = []
    path = '/org/ledup/advertisement0'
    primary_error = None
    try:
        try:
            bus = SystemMessageBus(bus_type=BusType.SYSTEM)
            await asyncio.wait_for(bus.connect(), 10)
        except AuthError as exc:
            raise RuntimeError('System D-Bus authentication rejected; check this user\'s system-bus access permissions') from exc
        except (TimeoutError, OSError, EOFError) as exc:
            raise RuntimeError('Cannot connect to the system D-Bus; run on a Linux host with BlueZ and Bluetooth hardware') from exc
        disconnected = asyncio.create_task(bus.wait_for_disconnect())
        pending.append(disconnected)
        bluez_lost = asyncio.Event()
        daemon_info = await bus.introspect('org.freedesktop.DBus', '/org/freedesktop/DBus')
        daemon = bus.get_proxy_object('org.freedesktop.DBus', '/org/freedesktop/DBus', daemon_info).get_interface(
            'org.freedesktop.DBus')

        def owner_changed(name, old_owner, new_owner):
            if name == 'org.bluez' and old_owner and old_owner != new_owner:
                bluez_lost.set()

        daemon.on_name_owner_changed(owner_changed)  # ty: ignore[unresolved-attribute]
        root = await bus.introspect('org.bluez', '/')
        objects = await bus.get_proxy_object('org.bluez', '/', root).get_interface(  # ty: ignore[unresolved-attribute]
            'org.freedesktop.DBus.ObjectManager').call_get_managed_objects()
        candidates = {p: interfaces for p, interfaces in objects.items()
                      if 'org.bluez.LEAdvertisingManager1' in interfaces}
        names = ', '.join(p.rsplit('/', 1)[-1] for p in candidates) or 'none'
        if adapter is not None:
            selected = f'/org/bluez/{adapter}'
            if selected not in candidates:
                raise RuntimeError(f'Adapter {adapter!r} has no LE advertising support; advertising-capable adapters: {names}')
        elif len(candidates) == 1:
            selected = next(iter(candidates))
        elif candidates:
            raise RuntimeError(f'Multiple advertising-capable adapters: {names}; select one with --adapter hci0')
        else:
            raise RuntimeError('No Bluetooth controller with LE advertising support; attach an advertising-capable adapter')
        interfaces = candidates[selected]
        if not interfaces.get('org.bluez.Adapter1', {}).get('Powered', Variant('b', False)).value:
            raise RuntimeError(f'{selected.rsplit("/", 1)[-1]} is powered off; power it on with bluetoothctl power on')
        properties = interfaces['org.bluez.LEAdvertisingManager1']
        # BlueZ reports remaining free slots, not total capacity.
        if 'SupportedInstances' in properties and properties['SupportedInstances'].value == 0:
            raise RuntimeError('No free Bluetooth advertising instances; stop your other advertising client')
        info = await bus.introspect('org.bluez', selected)
        manager = bus.get_proxy_object('org.bluez', selected, info).get_interface('org.bluez.LEAdvertisingManager1')
        bus.export(path, Advertisement())
        await manager.call_register_advertisement(path, {})  # ty: ignore[unresolved-attribute]
        registered = True
        timer = asyncio.create_task(asyncio.sleep(duration))
        release = asyncio.create_task(released.wait())
        lost = asyncio.create_task(bluez_lost.wait())
        pending.extend([timer, release, lost])
        done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        if disconnected in done:
            raise RuntimeError('System D-Bus disconnected before advertising completed')
        if lost in done:
            raise RuntimeError('BlueZ service disconnected before advertising completed; restart bluetoothd')
        if release in done:
            raise RuntimeError('BlueZ removed the advertisement before the requested duration')
    except DBusError as exc:
        primary_error = RuntimeError(f'{_BLUEZ_ERRORS.get(exc.type, "BlueZ advertising failed")} ({exc.type}: {exc.text})')
        raise primary_error from exc
    except (OSError, EOFError) as exc:
        primary_error = RuntimeError('System D-Bus disconnected during advertising')
        raise primary_error from exc
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error = None
        try:
            if registered and not released.is_set() and not bluez_lost.is_set() and bus.connected:
                await asyncio.wait_for(manager.call_unregister_advertisement(path), 5)  # ty: ignore[unresolved-attribute]
        except (Exception, asyncio.CancelledError) as exc:
            cleanup_error = exc
        finally:
            if bus is not None:
                bus.unexport(path)
                bus.disconnect()
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if cleanup_error is not None:
            if primary_error is None:
                if isinstance(cleanup_error, asyncio.CancelledError):
                    raise cleanup_error
                raise RuntimeError(f'Failed to unregister advertisement: {cleanup_error}') from cleanup_error
            print(f'Warning: advertisement cleanup failed: {cleanup_error}', file=sys.stderr)


def _number(value: str) -> int:
    try:
        return int(value, 16 if value.lower().startswith(('0x', '+0x', '-0x')) else 10)
    except ValueError:
        raise argparse.ArgumentTypeError('expected a decimal or 0x hexadecimal integer') from None


def _duration(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('duration must be finite and positive') from None
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError('duration must be finite and positive')
    return result


async def _send(payload, adapter, duration):
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await advertise(payload, adapter=adapter, duration=duration)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('products', help='list APK product IDs and pages')
    commands = sub.add_parser('commands', help='list product-specific numeric keys')
    commands.add_argument('--product', required=True)
    send = sub.add_parser('send', help='broadcast a catalog command (may affect multiple lights)')
    send.add_argument('--product', required=True)
    send.add_argument('--key', required=True, type=_number)
    send.add_argument('--dry-run', action='store_true')
    send.add_argument('--packet-id', type=_number)
    send.add_argument('--click-count', type=_number)
    send.add_argument('--duration', type=_duration, default=3.0)
    send.add_argument('--adapter')
    send.add_argument('--state', type=Path)
    args = parser.parse_args(argv)
    try:
        catalog = json.loads(Path(__file__).with_name('commands.json').read_text())
        if args.action == 'products':
            for product, data in catalog.items():
                print(product, f'page={data["page"]}', sep='\t')
            return 0
        if args.product not in catalog:
            parser.error(f'unknown product {args.product!r}; use products to list supported IDs')
        product = catalog[args.product]
        if args.action == 'commands':
            for command in product['commands']:
                print(command['key'], command['label'], f'page={product["page"]}', sep='\t')
            return 0
        if args.key not in {item['key'] for item in product['commands']}:
            parser.error(f'key {args.key} is not transmittable for {args.product}; use commands --product {args.product}')
        if not args.dry_run and (args.packet_id is not None or args.click_count is not None):
            parser.error('--packet-id and --click-count are only allowed with --dry-run')
        if args.packet_id is not None and not 1 <= args.packet_id <= 255:
            parser.error('--packet-id must be in 1..255')
        if args.click_count is not None and not 0 <= args.click_count <= 65535:
            parser.error('--click-count must be in 0..65535')
        if args.dry_run:
            packet = args.packet_id if args.packet_id is not None else 1
            click = args.click_count if args.click_count is not None else 0
        else:
            state = args.state or Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'ledup/counters.sqlite3'
            packet, click = reserve_counters(state, args.product, args.key)
        command = bytes([product['page'], args.key, 255, click & 255])
        payload = encode_payload(command, packet)
        if not args.dry_run:
            asyncio.run(_send(payload, args.adapter, args.duration))
        print(json.dumps({'command': command.hex(), 'packet_id': packet,
                          'manufacturer_id': 65520, 'payload': payload.hex(),
                          'advertised': not args.dry_run}))
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        print('Interrupted; advertising connection closed', file=sys.stderr)
        return 130
    except (OSError, EOFError, ValueError, sqlite3.Error, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

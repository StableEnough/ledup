"""LEDUP payload encoder, catalog, and counter rules (stdlib only; shared by the CLI)."""
import json
from pathlib import Path

CATALOG = json.loads(Path(__file__).with_name('commands.json').read_text())

_BIT_REVERSE = bytes(int(f'{value:08b}'[::-1], 2) for value in range(256))


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


def command_bytes(page: int, key: int, click: int) -> bytes:
    """Wire command: [page, key, ff, click low byte]."""
    return bytes([page, key, 255, click & 255])


def reserve(counters: dict, product: str, key: int) -> tuple[int, int]:
    """Return (packet, click) and advance both in counters; validates both before mutating."""
    click_name = f'click:{product}:{key}'
    bounds = (('packet', 1, 255, 1), (click_name, 0, 65535, 0))
    values = {}
    for name, low, high, default in bounds:
        value = counters.get(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'Corrupt counter {name!r}: expected {low}..{high}, got {value!r}')
        values[name] = value
    for name, low, high, _ in bounds:
        counters[name] = low if values[name] == high else values[name] + 1
    return values['packet'], values[click_name]

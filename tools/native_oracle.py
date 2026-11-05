"""Execute LEDUP 3.7's ARM64 encoder; never substitute Python protocol logic."""
import argparse
import hashlib
import io
import json
import random
import sys
import zipfile
from pathlib import Path

from elftools.elf.elffile import ELFFile
from unicorn import UC_ARCH_ARM64, UC_HOOK_CODE, UC_MODE_ARM, Uc, UcError
from unicorn.arm64_const import (
    UC_ARM64_REG_CPACR_EL1,
    UC_ARM64_REG_LR,
    UC_ARM64_REG_PC,
    UC_ARM64_REG_SP,
    UC_ARM64_REG_TPIDR_EL0,
    UC_ARM64_REG_X0,
    UC_ARM64_REG_X1,
    UC_ARM64_REG_X2,
    UC_ARM64_REG_X3,
    UC_ARM64_REG_X4,
    UC_ARM64_REG_X5,
)

ROOT = Path(__file__).resolve().parents[1]
NATIVE_SHA256 = "7974e293b5e959af6f35e242baf4ea8d8c27659882c2d78a09ce5c14afb493de"
COMMANDS = tuple(bytes.fromhex(c) for c in (
    "0110ff00", "0105ff00", "0105ffff", "fe0eff00", "ff0effff", "fd10ff00", "127e83a5",
))


class NativeOracle:
    STACK, HEAP, DATA, TLS, RETURN = 0x1000000, 0x2000000, 0x3000000, 0x4000000, 0x5000000
    REDIRECTS = {0x3b960: 0x1aa5c, 0x3b990: 0x1a894, 0x3b9a0: 0x1a8ac, 0x3b9b0: 0x1aabc}

    def __init__(self, xapk):
        with zipfile.ZipFile(xapk) as outer:
            with zipfile.ZipFile(io.BytesIO(outer.read("config.arm64_v8a.apk"))) as split:
                native = split.read("lib/arm64-v8a/libnativelib.so")
        digest = hashlib.sha256(native).hexdigest()
        if digest != NATIVE_SHA256:
            raise ValueError(f"Native hash mismatch: expected {NATIVE_SHA256}, got {digest}")
        self.uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        elf = ELFFile(io.BytesIO(native))
        for segment in elf.iter_segments():
            if segment['p_type'] != 'PT_LOAD':
                continue
            start = segment['p_vaddr'] & ~4095
            end = (segment['p_vaddr'] + segment['p_memsz'] + 4095) & ~4095
            flags = segment['p_flags']
            # ELF PF_R/PF_W/PF_X -> Unicorn READ/WRITE/EXEC.
            perms = (1 if flags & 4 else 0) | (2 if flags & 2 else 0) | (4 if flags & 1 else 0)
            self.uc.mem_map(start, end - start, perms)
            self.uc.mem_write(segment['p_vaddr'], segment.data())
        for address, size in ((self.STACK, 0x10000), (self.HEAP, 0x10000),
                              (self.DATA, 4096), (self.TLS, 4096)):
            self.uc.mem_map(address, size, 3)
        self.uc.mem_map(self.RETURN, 4096, 5)
        self.uc.reg_write(UC_ARM64_REG_CPACR_EL1, 3 << 20)
        self.uc.reg_write(UC_ARM64_REG_TPIDR_EL0, self.TLS)
        self.uc.mem_write(self.TLS + 40, bytes.fromhex('123456789abcdef0'))
        self.uc.hook_add(UC_HOOK_CODE, self._instruction)
        self.allocations = {}
        self.fill = 0

    def _instruction(self, uc, address, size, _):
        if address in self.REDIRECTS:
            uc.reg_write(UC_ARM64_REG_PC, self.REDIRECTS[address])
            return
        if address == 0x3b940:
            raise RuntimeError('Native stack-canary failure')
        if address == 0x3b9c0:
            length = uc.reg_read(UC_ARM64_REG_X0)
            if not 0 < length <= 4096 or self.allocations:
                raise RuntimeError(f'Unexpected native allocation: {length}')
            self.allocations[self.HEAP] = length
            uc.mem_write(self.HEAP, bytes([self.fill]) * length)
            uc.reg_write(UC_ARM64_REG_X0, self.HEAP)
        elif address == 0x3b9d0:
            dest, src, length = (uc.reg_read(r) for r in (UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2))
            if dest != self.DATA + 128 or length != 16 or src != self.HEAP + 15:
                raise RuntimeError('Unexpected native memcpy')
            uc.mem_write(dest, bytes(uc.mem_read(src, length)))
        elif address == 0x3b9e0:
            pointer = uc.reg_read(UC_ARM64_REG_X0)
            if pointer not in self.allocations:
                raise RuntimeError('Native delete of unowned allocation')
            del self.allocations[pointer]
        else:
            # Only the reviewed encoder/helper instruction ranges are executable.
            if not (0x1a598 <= address < 0x1a894 or 0x1a894 <= address < 0x1ac00):
                raise RuntimeError(f'Unexpected native execution at {address:#x}')
            return
        uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_LR))

    def encode(self, command, packet_id, fill=0):
        if len(command) != 4 or not 1 <= packet_id <= 255:
            raise ValueError('Expected four command bytes and packet ID 1..255')
        self.fill = fill
        self.allocations.clear()
        self.uc.mem_write(self.STACK, bytes([fill]) * 0x10000)
        self.uc.mem_write(self.DATA, b'\x5a\x00\x10')
        self.uc.mem_write(self.DATA + 64, b'\xff\xff\xff' + bytes([packet_id]) + command)
        self.uc.mem_write(self.DATA + 128, b'\xcc' * 16)
        for reg, value in ((UC_ARM64_REG_X0, self.DATA), (UC_ARM64_REG_X1, 3),
                           (UC_ARM64_REG_X2, 15), (UC_ARM64_REG_X3, self.DATA + 64),
                           (UC_ARM64_REG_X4, 8), (UC_ARM64_REG_X5, self.DATA + 128),
                           (UC_ARM64_REG_SP, self.STACK + 0xff00), (UC_ARM64_REG_LR, self.RETURN)):
            self.uc.reg_write(reg, value)
        try:
            self.uc.emu_start(0x1a598, self.RETURN, timeout=1000000, count=100000)
        except UcError as exc:
            raise RuntimeError(f'Native emulation failed at {self.uc.reg_read(UC_ARM64_REG_PC):#x}: {exc}') from exc
        if self.uc.reg_read(UC_ARM64_REG_PC) != self.RETURN or self.allocations:
            raise RuntimeError('Native encoder did not return cleanly within execution limits')
        return bytes(self.uc.mem_read(self.DATA + 128, 16))

    def stable_encode(self, command, packet_id):
        result = self.encode(command, packet_id, 0)
        for fill in (0x55, 0xff):
            if self.encode(command, packet_id, fill) != result:
                raise RuntimeError('Discarded allocation/stack padding affects native output')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xapk', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--write-vectors', type=Path)
    mode.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    oracle = NativeOracle(args.xapk)
    cases = [(command, packet) for command in COMMANDS for packet in (1, 255)]
    if args.write_vectors:
        vectors = [{'command': c.hex(), 'packet_id': p, 'payload': oracle.stable_encode(c, p).hex()} for c, p in cases]
        args.write_vectors.parent.mkdir(parents=True, exist_ok=True)
        args.write_vectors.write_text(json.dumps({'native_sha256': NATIVE_SHA256, 'cases': vectors}, indent=2) + '\n')
        print(f'Wrote {len(vectors)} native-only vectors; discarded-prefix fill independence verified')
        return
    sys.path.insert(0, str(ROOT))
    from custom_components.ledup.protocol import encode_payload
    fixtures = json.loads((ROOT / 'tests/fixtures/native_vectors.json').read_text())
    if fixtures['native_sha256'] != NATIVE_SHA256:
        raise ValueError('Fixture native hash mismatch')
    for row in fixtures['cases']:
        native = oracle.stable_encode(bytes.fromhex(row['command']), row['packet_id'])
        if native.hex() != row['payload']:
            raise AssertionError(f'Native fixture changed: {row}')
    cases = [(bytes.fromhex(r['command']), r['packet_id']) for r in fixtures['cases']]
    cases += [(c, p) for c in COMMANDS for p in range(1, 256)]
    rng = random.Random(0)
    cases += [(rng.randbytes(4), rng.randint(1, 255)) for _ in range(256)]
    catalog = json.loads((ROOT / 'custom_components/ledup/commands.json').read_text())
    cases += [(bytes([product['page'], item['key'], 255, 0]), 1)
              for product in catalog.values() for item in product['commands']]
    for command, packet in cases:
        expected = oracle.stable_encode(command, packet)
        actual = encode_payload(command, packet)
        if actual != expected:
            raise AssertionError(f'command={command.hex()} packet={packet}: native={expected.hex()} python={actual.hex()}')
    print(f'PASS: {len(cases)} native/Python cases; three padding fills per case')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, AssertionError, KeyError, zipfile.BadZipFile) as exc:
        sys.exit(f'Oracle error: {exc}')

# LEDUP script control

Python control for the LEDUP 3.7 app's 16 product types, using local Linux BLE manufacturer advertising. No pairing, device MAC, GATT connection, cloud service, or Home Assistant component is required.

## Try without Bluetooth

Install [uv](https://docs.astral.sh/uv/), then run from this directory:

```sh
uv sync
uv run python ledup.py products
uv run python ledup.py commands --product wtrtd200f
uv run python ledup.py send --product wtrtd200f --key 5 --dry-run
```

Choose the product ID from your controller/tree's QR code or identifier, **not its appearance**. `wtrtd200f` is an example, not a detected device. Numeric keys accept decimal or `0x` hexadecimal. Catalog labels preserve the app's meanings: **Switch is a toggle/button, not proven idempotent on/off**. Light/Clock labels describe the app's icons without inventing brightness or timer arguments. Phone-only shake selectors are excluded.

Dry-run returns deterministic JSON without connecting to D-Bus, creating state, or transmitting. Optional `--packet-id 1 --click-count 0` overrides are dry-run only. Example Steady payload:

```json
{"command":"0105ff00","packet_id":1,"manufacturer_id":65520,"payload":"6db643645d16fb6d1a1cffbdaefab7aa","advertised":false}
```

## Send near the lights

Use a Linux machine near the lights with an LE-advertising-capable Bluetooth adapter, BlueZ, a running system D-Bus/bluetoothd, and permission to register advertisements. On Debian/Ubuntu, **on that Bluetooth-equipped host**:

```sh
sudo apt install bluez
sudo systemctl start bluetooth
bluetoothctl power on
uv run python ledup.py send --product YOUR_PRODUCT --key YOUR_KEY --adapter hci0
```

Replace `YOUR_PRODUCT` and `YOUR_KEY` with catalog values. Without `--adapter`, the sole advertising-capable controller is selected; multiple candidates require an explicit choice. The script never powers a controller on or removes another application's advertisement. Permission failures require checking the host's D-Bus policy; do not run arbitrary scripts as root to bypass them.

BlueZ's `SupportedInstances` counts remaining free slots, not total capacity. Another active advertiser does not block a send when a free slot remains.

Each send advertises for `--duration 3.0` seconds by default, requests a 100 ms advertising interval, then unregisters. Older BlueZ stacks may ignore interval properties: use `sudo btmon` to inspect the interval accepted by the controller rather than assuming parity. Measuring actual over-air timing requires a separate receiver. SIGINT/SIGTERM close the advertiser. There are no automatic retries: repeating a toggle can undo the intended action.

`TxPower` is intentionally omitted, leaving power selection to BlueZ and the controller; no TX-power AD field is added. Reads of absent optional properties receive standard D-Bus errors without traceback logging.

Successful sends print the same JSON shape as dry-run, with `advertised: true` only after the advertising lifecycle completes. This means **transmitted, not acknowledged by the lights**. Errors exit 1, usage errors 2, interruptions 130 (including SIGINT/SIGTERM during advertisement cleanup). Rejected system D-Bus authentication produces a permission-focused error without a traceback; a connection closed during authentication exits 1 rather than hanging. Broadcasts may affect multiple compatible lights in range; there is no per-device targeting or cryptographic authentication.

Packet and per-product/key click counters persist atomically in `${XDG_STATE_HOME:-~/.local/state}/ledup/counters.sqlite3`; use `--state PATH` for an isolated installation. Packet IDs wrap 255→1, click counts 65535→0 (low byte on the wire). Counters are committed before advertising, including failed attempts; corrupt state fails rather than silently resetting. Keep this file stable across one-shot invocations.

For Python callers, `encode_payload(command: bytes, packet_id: int) -> bytes` is pure stdlib and returns exactly 16 bytes. `await advertise(payload, adapter=None, duration=3.0)` transmits an already encoded payload; callers using it directly must manage counters themselves.

## Verification

```sh
uv sync --group dev
uv run python -m unittest discover -s tests
uv run --group dev python tools/native_oracle.py --xapk LEDUP_3.7_APKPure.xapk --verify
```

Tests need no Bluetooth, root, APK, or emulator:

- Checked-in native fixtures and temporary SQLite databases cover encoding and counters.
- Authentication regressions launch the actual CLI against local sockets that reject authentication or disconnect mid-response.
- An isolated D-Bus daemon with simulated BlueZ checks that one occupied slot plus one free slot permits advertising, optional-property reads return `UnknownProperty`, writes to read-only properties return `PropertyReadOnly`, and interruption during cleanup exits 130 and closes the advertising client's bus connection without a traceback. Only this check is skipped if `dbus-daemon` is unavailable.
- The explicit oracle uses Unicorn to execute the actual ARM64 C++ encoder from the XAPK's ABI split. It verifies the native SHA-256 before using version-specific addresses, compares 2,287 cases against the port, and varies discarded allocation/stack padding across three fill patterns per case. It hooks allocation/copy/delete only; CRC and whitening execute natively.

Regenerate the 14 native-only golden vectors (does not import the Python encoder):

```sh
uv run --group dev python tools/native_oracle.py --xapk LEDUP_3.7_APKPure.xapk --write-vectors tests/fixtures/native_vectors.json
```

Verified software gates: native parity, offline CLI behavior, counter isolation/rollover/concurrency and malformed-state failure, and missing-BlueZ error handling.

Real-controller smoke checks passed on an Intel USB adapter (`hci0`, BlueZ 5.82), using the **sample** `wtrtd200f` Steady command, not a detected tree product:

- The exact manufacturer payload reached the controller, the controller accepted a 100 ms interval, and advertising was disabled/removed after the default three-second send.
- Normal sends exited 0 with empty stderr; SIGINT and SIGTERM exited 130 with no traceback and restored the active advertising count to zero.
- An invalid adapter exited 1 without success JSON.
- `TxPower` remained unspecified and advertising data remained 20 bytes, including its AD wrapper.

**Still unverified:** over-air reception/timing and physical light response. `btmon` establishes host/controller traffic, not individual RF transmissions; a separate receiver is needed for RF-byte capture and actual packet cadence. When the tree is accessible, select its actual product ID and observe steady/effect, Switch, another mode, and repeated mode commands across separate CLI invocations. Record the actual product ID and actions; support in the catalog is not proof that every product was physically tested.

## Protocol provenance

For a port to another language, use [PROTOCOL.md](PROTOCOL.md): the complete byte layout, CRC and whitening pseudocode, counter rules, advertising transport, worked intermediate values, and native-oracle provenance. Reuse `commands.json` and `tests/fixtures/native_vectors.json` directly; no APK decompilation is required.

Source: supplied `LEDUP_3.7_APKPure.xapk`, decompiled `LEAdvertiser`, `KeyItem`, and `KeyHelper`, plus ARM64 `libnativelib.so` SHA-256 `7974e293b5e959af6f35e242baf4ea8d8c27659882c2d78a09ce5c14afb493de`. Static catalog data is in `commands.json`; the APK is not needed at runtime.

Commands are `[page, key, ff, click_low_byte]`; the native input body is `ff ff ff packet_id command`, address `5a 00 10`, manufacturer ID `0xfff0` (65520). The encoder uses CRC-16/0x1021 and fixed whitening seeds 0x3f and 0x25; the outer stream includes 120 discarded prefix bits. These are not encryption/authentication. The JNI wrapper adds prefix offset 15; TLS+40 is a stack canary, not randomness.

BlueZ API references: [advertisement](https://github.com/bluez/bluez/blob/master/doc/org.bluez.LEAdvertisement.rst), [advertising manager](https://github.com/bluez/bluez/blob/master/doc/org.bluez.LEAdvertisingManager.rst).

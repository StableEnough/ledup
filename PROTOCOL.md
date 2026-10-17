# LEDUP 3.7 wire protocol and porting guide

This document specifies the reconstructed command encoding independently of Python. A new implementation needs this document and `commands.json`; use `tests/fixtures/native_vectors.json` as its byte-for-byte acceptance data. No APK decompilation, Android runtime, or native library is needed to implement or run a port.

Scope: the supplied LEDUP **3.7** app's command-transmit path. Encoding has been compared with actual ARM64 native execution; physical reception by the lights has **not** been tested. Catalog entries are app-supported commands, not claims that all products have been tested. Other app/firmware versions may differ.

## 1. Inputs and product catalog

All unqualified byte strings below are hexadecimal. Arrays contain unsigned bytes; counters and intermediate arithmetic use integers, not signed characters. `||` means byte-array concatenation. Array slices have an inclusive start and exclusive end. Bit 0 is the least significant bit. Bit reversal never changes byte-array order unless explicitly stated.

`commands.json` is the authoritative, static, language-neutral command catalog. Its root is keyed by product ID; each value has a numeric `page` and an ordered `commands` array of `{ "key": integer, "label": string }`. Preserve numeric keys: labels are presentation, not protocol identifiers. Pages and keys fit in one unsigned byte.

| Product ID | Page (decimal) |
| --- | ---: |
| wtrtd200f | 1 |
| ptr043f | 2 |
| ptr002m | 3 |
| wtr116f | 4 |
| ptrs052f | 5 |
| ctr015f | 6 |
| ctr010f | 7 |
| nctr010f | 8 |
| nptr043fm | 9 |
| nctr015f | 10 |
| ctr016f | 11 |
| ctr018f | 12 |
| nctr016f | 13 |
| wtr136f | 253 |
| wtr005b | 254 |
| ctr003b | 255 |

Choose a product from the tree/controller's identifier, not its appearance. Excluded UI entries (`createNone()` and nctr010f's key -1 shake selector) do not transmit. Icon-only labels come from drawable names; STC directional controls have distinct labels. **Switch is a button/toggle, not proven idempotent on/off.** Do not invent brightness/timer arguments from icon names.

Construct exactly four command bytes:

```text
command = [page, key, 0xff, click_count AND 0xff]
```

Packet ID is an integer from 1 through 255. The encoder accepts any four command bytes, but a user-facing controller should only offer transmittable catalog keys. Known first presses (click count 0):

| Product / action | Command |
| --- | --- |
| wtrtd200f / Switch, key 16 | 01 10 ff 00 |
| wtrtd200f / Steady, key 5 | 01 05 ff 00 |
| wtr005b / Switch, key 14 | fe 0e ff 00 |
| ctr003b / Switch, key 14 | ff 0e ff 00 |

### Counter semantics

The app starts packet IDs at 1 and each key's click counter at 0. A press uses the current values, then advances packet 255→1 and click 65535→0. Only the click counter's low byte goes on the wire. Key adapters send on press-down; release sends no extra packet.

Our one-shot CLI persists counters across processes (the app's counters are in memory). For equivalent script behavior, reserve the next global packet ID and next per-`(product,key)` click count atomically, persist their successors **before attempting transmission**, then encode with the reserved values. A failed transmission still consumes its reservation. Do not silently reset corrupt state or automatically retry toggles. SQLite is an implementation choice, not a wire requirement. Sharing the existing CLI state requires the `counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL)` table, names `packet` and `click:<product>:<decimal-key>`, and the same atomic transaction.

The lights' deduplication policy and tolerance of counter resets have not been established; do not infer them from the app's counters.

## 2. Framing and CRC

Fixed encoder address bytes are `5a 00 10`. This is **not** a Bluetooth device MAC or a selectable target. Form the eight-byte body:

```text
address = [0x5a, 0x00, 0x10]
body = [0xff, 0xff, 0xff, packet_id] || command
```

Define `reverse_bits(value, width)` as reversal of exactly `width` bits, with zero bits retained. For example, `reverse_bits(0x01,8)=0x80`; `reverse_bits(0x1234,16)=0x2c48`.

Compute CRC using this algorithm, not a library preset inferred from its polynomial alone:

```text
crc = 0xffff
crc_input = reverse_byte_order(address) || map(reverse_bits(byte,8), body)
for byte in crc_input:
    crc = crc XOR (byte << 8)
    repeat 8 times:
        high_bit = crc AND 0x8000
        crc = (crc << 1) AND 0xffff
        if high_bit != 0:
            crc = crc XOR 0x1021
crc = reverse_bits(crc,16) XOR 0xffff
crc_bytes = [crc AND 0xff, (crc >> 8) AND 0xff]
```

The address is reversed in **byte order but not bit-reversed** for CRC input. Body bytes are bit-reversed individually before CRC input. Neither prefix nor padding contributes to CRC. Final CRC is stored little-endian. These distinctions are easy to lose in a port.

Build the following logical buffer **before whitening**:

| Zero-based offsets | Length | Contents |
| --- | ---: | --- |
| 0–14 | 15 | Discarded padding; zero bytes are sufficient |
| 15–17 | 3 | Bit-reversed bytes of raw prefix `71 0f 55`, giving `8e f0 aa` |
| 18–20 | 3 | Reverse address byte order, then reverse each byte's bits: `08 00 5a` |
| 21–28 | 8 | Body, unchanged (not bit-reversed here) |
| 29–30 | 2 | CRC, little-endian |

## 3. Whitening

Whitening is XOR with a deterministic bit stream, **not encryption or authentication**. Each pass starts a separate seven-bit state from its seed:

```text
state = [1, (seed >> 5) AND 1, (seed >> 4) AND 1,
            (seed >> 3) AND 1, (seed >> 2) AND 1,
            (seed >> 1) AND 1, seed AND 1]

function whiten(bytes, seed):
    initialize state as above
    for each byte in bytes, in array order:
        for bit = 0 through 7:
            [s0,s1,s2,s3,s4,s5,s6] = state
            byte = byte XOR (s6 << bit)
            state = [s6,s0,s1,s2,s3 XOR s6,s4,s5]
    return modified bytes
```

Each transition uses **old** state values simultaneously. The stream bit is old `s6`, and bytes are processed **LSB first**. Maintain state across all bytes in a pass.

1. Inner whitening: apply seed **0x3f** to offsets `[18:31]`, exactly 13 bytes (transformed address, body, CRC). Leave padding and three-byte prefix unchanged.
2. Outer whitening: apply seed **0x25** to **all 31 bytes**, including the discarded padding.
3. Return offsets `[15:31]`: exactly **16 manufacturer payload bytes**.

A port may omit allocating padding, but must advance the outer stream **120 bits** before processing the returned bytes. Starting outer whitening at the returned prefix is wrong. The native function leaves discarded allocation padding uninitialized; it is not entropy because this stream is independent of the input bytes. Native execution with fills 00, 55, and ff produced identical returned payloads.

### Worked example

Inputs: wtrtd200f Steady, key 5, click count 0, packet ID 1.

| Stage | Hexadecimal bytes |
| --- | --- |
| Command | `0105ff00` |
| Body | `ffffff010105ff00` |
| Final CRC, stored little-endian | `9d1e` |
| Returned region before either whitening pass | `8ef0aa08005affffff010105ff009d1e` |
| Returned region after inner whitening | `8ef0aacf8d88a85ec2a667b58a318c56` |
| Final 16-byte payload | `6db643645d16fb6d1a1cffbdaefab7aa` |

This payload matches the checked-in native fixture. Do not append or prepend the manufacturer ID to the encoder's return value.

## 4. Bluetooth advertising transport

The app transmits BLE **manufacturer advertising**, not a GATT write. There is no service UUID, pairing operation, target MAC, or acknowledgment in this send path. It advertises a command repeatedly for about three seconds, then stops. A new command stops the previous advertisement. A port should remove its advertisement on normal completion, interruption, or failure; do not leave an unbounded advertiser running.

| Setting | Value / qualification |
| --- | --- |
| Manufacturer/company ID | Decimal **65520**, hexadecimal **0xfff0**, serialized `f0 ff` |
| Manufacturer payload | Exactly the encoder's 16 bytes |
| Advertiser type | Connectable / BlueZ `peripheral` |
| App duration | 3 seconds |
| App Android settings | Low latency (mode 2), TX power level 3; not a measured dBm value |
| Linux port interval | Requests min=max 100 ms; **not physically measured**; older stacks may ignore it |

If an API accepts manufacturer ID and bytes separately, pass `{0xfff0: payload}`; the Bluetooth stack adds the company ID. If constructing raw advertising-data structures, the manufacturer-specific structure for the worked example is:

```text
13 ff f0 ff 6d b6 43 64 5d 16 fb 6d 1a 1c ff bd ae fa b7 aa
```

`13` is the length (19 bytes follow: type + company ID + payload), `ff` is the Manufacturer Specific Data AD type, `f0 ff` is the little-endian company ID, and the last 16 bytes are the encoded payload. This is **one AD structure, not the complete BLE radio packet**; the platform supplies other required fields. The Android data builder does not add a local name, service UUID, or TX-power AD field. Do not duplicate `f0 ff` inside the manufacturer payload.

For Linux use BlueZ `org.bluez.LEAdvertisingManager1` on the system bus, export `org.bluez.LEAdvertisement1`, and register/unregister that object. `ManufacturerData` has D-Bus signature `a{qv}`; value `0xfff0` maps to an `ay` variant holding the 16 bytes. Optional `MinInterval`/`MaxInterval` properties are uint32 milliseconds. A `Release()` notification means BlueZ already unregistered the object. Early release, service/bus loss, denied access, or registration failure is not a successful send. A success result means the advertising lifecycle completed, not that a light changed state.

Other language/platform BLE APIs may impose advertising or background-execution limits; those need platform validation, not a different encoder. Broadcast commands can affect multiple compatible lights in range. No per-device identity or secure pairing mechanism has been established in this path.

## 5. Port acceptance and evidence preservation

1. Read `commands.json` directly; do not scrape labels or reconstruct Android resources. It contains 16 products and 232 transmittable commands.
2. Implement the encoder above. Load every `cases` entry from `tests/fixtures/native_vectors.json`: decode `command` from hex, encode using `packet_id`, and require exact equality with hex-decoded `payload`. These **14 vectors were generated by actual native execution**, not by the Python port, and include packet 1/255, click-byte 00/ff, high page values, and arbitrary command bytes.
3. Preserve input validation and counter boundaries: command length exactly 4, packet 1..255, packet wrap 255→1, full click wrap 65535→0, low-byte click truncation, independent per-product/key counters, atomic reservations, and no successful output on send failure.
4. For broader independent evidence, reuse `tools/native_oracle.py`. It executes the APK routine using Unicorn and compares 2,287 cases against the Python encoder: 14 fixtures, 7 commands × 255 packet IDs, 256 deterministic random cases, and all 232 catalog entries at first press. Each case checks three padding fills. A new-language port can consume oracle-generated JSON vectors or compare its output with native execution; comparing only with Python is useful but weaker evidence. The standalone oracle's `--verify` currently imports `ledup.py`; adapting that comparison is not reverse engineering.
5. Validate each target platform on real hardware: capture company ID and payload bytes, duration, configured interval, and interruption cleanup; use a separate receiver to measure over-air timing, then observe actual product-specific effects and repeated commands across separate process invocations. The Python controller's host/controller smoke checks passed (see [README.md](README.md)); **over-air reception/timing and physical tree response remain unverified**.

Keep this document, `commands.json`, native JSON fixtures, and `tools/native_oracle.py` with any future port. Keep the original XAPK if you want to rerun the independent native oracle; it is not needed for normal operation or fixture-based acceptance. The implementation and documentation do not depend on temporary decompiled files under `/tmp`.

### Artifact and native entry points

Source archive: `LEDUP_3.7_APKPure.xapk`. Native member: `config.arm64_v8a.apk` → `lib/arm64-v8a/libnativelib.so` (nested ZIPs). Its required SHA-256 is:

```text
7974e293b5e959af6f35e242baf4ea8d8c27659882c2d78a09ce5c14afb493de
```

Java provenance: `com.tuner168.ledup.utils.LEAdvertiser` (framing/advertising), `com.tuner168.ledup.KeyItem` (command/click counter), `com.tuner168.ledup.utils.KeyHelper` (catalog), and key adapters (press-down dispatch). These app classes were in `classes2.dex`. The native library is in the ABI split, not downloaded at runtime.

The hash-gated native oracle preserves these virtual addresses; they are **not valid for another binary**:

| Function / purpose | Virtual address |
| --- | --- |
| JNI wrapper | `0x19e20` |
| C++ `_Z14get_rf_payloadPKhiiS0_iPh` | `0x1a598` |
| Reverse 8 / 16 bits | `0x1a894` / `0x1a8a0` |
| CRC | `0x1a8ac` |
| Whitening init / output / encode | `0x1aa5c` / `0x1aa90` / `0x1aabc` |

C++ call: `get_rf_payload(address_pointer, 3, 15, body_pointer, 8, output_pointer)` with argument registers `x0,w1,w2,x3,w4,x5`. The JNI wrapper supplies constant prefix offset 15. Map ELF `PT_LOAD` segments including BSS, separate stack/heap/data/TLS, enable SIMD, and set `TPIDR_EL0` with a stable canary at TLS+40. That TLS value is a **stack canary, not a random protocol seed**.

The oracle redirects native PLT entries `0x3b960→0x1aa5c`, `0x3b990→0x1a894`, `0x3b9a0→0x1a8ac`, and `0x3b9b0→0x1aabc`. Only allocation (`0x3b9c0`), memcpy (`0x3b9d0`), deletion (`0x3b9e0`), and stack failure (`0x3b940`, abort) are handled outside the native code. CRC and whitening are **not** replaced with Python. Execution is bounded and unexpected execution/memory failures abort.

If revisiting constants, translate virtual addresses through their containing load segment: `file_offset = p_offset + virtual_address - p_vaddr`. In this binary the signed initialization shift lanes at VA `0x10bd0` are **[-5,-4,-3,-2]**; whitening byte-shift lanes at `0x10be0` are [2,3,4,5], masks at `0x10bf0` are [4,8,16,32]. Do not assume virtual addresses always equal file offsets, or infer randomness from a port mismatch.

BlueZ references: [advertisement contract](https://github.com/bluez/bluez/blob/master/doc/org.bluez.LEAdvertisement.rst), [advertising manager](https://github.com/bluez/bluez/blob/master/doc/org.bluez.LEAdvertisingManager.rst).

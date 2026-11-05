# AGENTS.md

Local BLE advertising control for LEDUP lights (Python CLI, `ledup.py`). Read `README.md` for usage and `PROTOCOL.md` for the byte-level protocol.

## Commands

```sh
uv sync --group dev                              # install deps (Python 3.14 only)
uv run python -m unittest discover -s tests -v   # tests; no Bluetooth/root/APK needed
uv run ruff check .                              # lint (CI runs this too)
uv run ty check                                  # type check ledup.py + tools/ (CI runs this too)
uv run python ledup.py send --product wtrtd200f --key 5 --dry-run   # safe: never transmits
```

CI (`.github/workflows/ci.yml`, GitHub Actions) runs `uv sync --locked --group dev`, `ruff check`, `ty check`, the unittest suite, hassfest, and the HACS validator. Keep `uv.lock` committed and in sync (`uv lock` after editing `pyproject.toml`).

## Layout

- `ledup.py`: the CLI: SQLite counters, BlueZ advertising over D-Bus.
- `custom_components/ledup/`: HACS integration (buttons sending through an ESPHome relay action). `protocol.py` holds the encoder (CRC + whitening), catalog loader, and counter math shared with the CLI; `commands.json` is the static product/command catalog extracted from the app.
- `esphome/ledup.yaml`: ESPHome snippet adding the `ledup_advertise` relay action to a Bluetooth proxy.
- `tests/`: unittest suite; `tests/fixtures/native_vectors.json` holds golden vectors from the native encoder.
- `tools/native_oracle.py`: runs the app's ARM64 encoder under Unicorn to verify the Python port. Needs the untracked `LEDUP_3.7_APKPure.xapk`.
- `tools/probe_products.sh`: transmits Switch to every product to find an unknown product ID. Real hardware only.

## Rules

- **Never transmit in tests, examples, or automation.** Use `--dry-run`. `send` without it broadcasts BLE advertisements that can toggle real lights in range, and sends are not idempotent (no retries by design).
- Do not run `tools/probe_products.sh` without `--dry-run` unless the user asks.
- Do not commit the XAPK/APK (gitignored) or any `*.sqlite3` state.
- Python is capped at `>=3.14,<3.15` because `dbus-next==0.2.3` breaks on 3.15. Dependabot (`.github/dependabot.yml`) does not bump `requires-python` or `.python-version`; lift both together when migrating to `dbus-fast`.
- Do not change the encoder, counters, or `commands.json` semantics without re-verifying against `native_vectors.json`, and keep `PROTOCOL.md` in sync.
- Counters are committed before advertising, including failed attempts; this is intentional.
- Ruff ignores `F821`/`F722`/`UP037` because `dbus-next` annotations are D-Bus signature strings (`'s'`, `'u'`), not types. Do not "fix" them. For the same reason `ledup.py` carries targeted `# ty: ignore[...]` comments on those annotations and on dbus-next's dynamic proxy methods (`call_*`, `on_*`); `tests/` is excluded from `ty` because it passes invalid types on purpose.
- Style: match the surrounding code (single quotes in `ledup.py`, terse comments). No formatter is enforced; do not reformat whole files.
- Claims in docs must match what was actually verified; keep the README's "Still unverified" section honest.

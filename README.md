# Oraimo Watch 6R SDK & Local Dashboard

[![CI](https://github.com/eyadmkv/oraimo-watch6r/actions/workflows/ci.yml/badge.svg)](https://github.com/eyadmkv/oraimo-watch6r/actions/workflows/ci.yml)
[![Pack extension](https://github.com/eyadmkv/oraimo-watch6r/actions/workflows/pack.yml/badge.svg)](https://github.com/eyadmkv/oraimo-watch6r/actions/workflows/pack.yml)

A read-only Python SDK for the Oraimo Watch 6R smartwatch (BLE protocol `MOYOUNG-V2`), plus a fully local personal dashboard: a Python HTTP bridge that owns the only BLE connection, and a Chrome Manifest V3 extension that renders watch data on the New Tab page.

Nothing is published, hosted, or uploaded anywhere. The bridge binds to `127.0.0.1` only, the extension is never sent to the Chrome Web Store, and every BLE operation is a read.

> **Hardware status:** the BLE transport and decoding logic are unit tested against captured payloads, but some decoders (marked *provisional* below) are inferred from a limited number of captures and have not been fully confirmed against the watch. Unavailable data is never faked: the dashboard shows "—", not zero.

## Architecture

```text
┌──────────────┐   BLE (MOYOUNG-V2)   ┌─────────────────┐   HTTP 127.0.0.1:8765   ┌────────────────────┐
│ Oraimo Watch │ ◄──────────────────► │  Python bridge  │ ◄────────────────────── │  Chrome extension  │
│      6R      │   one connection,    │  (bridge/)      │   token + origin        │  (chrome-extension)│
└──────────────┘   read-only, on PC   └─────────────────┘   allow-list only      └────────────────────┘
```

The watch accepts **one** BLE connection at a time — turn off the phone's Bluetooth while using the dashboard. The bridge owns that connection; the extension never talks BLE itself, it only sends HTTP commands to the running bridge.

## Repository layout

```text
src/oraimo_watch6r/   SDK package (asyncio + bleak)
examples/             small usage examples
tests/                hardware-independent unit tests (unittest + node --test)
bridge/               local loopback HTTP bridge for the dashboard
chrome-extension/     Chrome new-tab dashboard (Manifest V3)
.github/workflows/    CI (tests) and extension packaging (.crx/.zip)
readonly_probe.py     standalone diagnostic reference, intentionally outside the SDK
live_watch.py, probe_watch.py, bp_probe.py, passive_probe.py, inspect_watch.py
                      one-off protocol probe scripts used during reverse engineering
```

## Requirements

- Python ≥ 3.12 with [bleak](https://github.com/hbldh/bleak) (`pip install -e .` pulls it in)
- Node.js ≥ 20 (only for the extension API tests and local packing)
- A Windows PC with Bluetooth, paired with the watch (bleak's backend is Windows-focused here)
- Chromium-based browser with extension support for the dashboard

## Installation

From the project root (a virtual environment is recommended):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Bleak is the only runtime dependency. The test suite uses Python's built-in `unittest` and does not require a watch or pytest.

## SDK quick start

```python
import asyncio
from oraimo_watch6r import WatchClient


async def main():
    devices = await WatchClient.scan()
    if not devices:
        print("Watch not found")
        return

    async with WatchClient(devices[0].address, name=devices[0].name) as watch:
        print(await watch.device_info())
        print(await watch.read_battery())
        print(await watch.get_step_goal())
        print(await watch.get_heart_rate_history(0))


asyncio.run(main())
```

Or run the bundled example, which scans, connects, verifies the manufacturer string, reads device info / battery / step goal / one block of heart-rate history, and reads yesterday's activity:

```powershell
python .\examples\read_basic_info.py
```

Streams:

- `watch.step_count_stream()` — compact step notifications from `FEA1`
- `watch.activity_stream()` — full live activity notifications from `FEE1`
- `python .\examples\stream_heart_rate.py` — standard Heart Rate Measurement (`2A37`); the watch may need to be actively measuring before it emits these

### Known device profile

The tested device identifies as:

| Field | Value |
| --- | --- |
| Manufacturer | `MOYOUNG-V2` |
| Firmware | `JLQFNLKV3.5` |
| Software | `823-8AW4-1.3.0` |
| Advertised name | `oraimo Watch 6R_5947` |

The SDK discovers the watch by advertised name; it never hardcodes a BLE address.

### What the SDK implements

- Basic info: manufacturer, firmware, software, battery
- Live activity and notifications from `FEE1`; compact steps from `FEA1`
- Standard Heart Rate Measurement notifications from `2A37`
- Watch face and its layout; clock format, units, language, step target, dominant hand
- Quick look, sedentary reminder, DND and quick look schedule, move reminder
- Raw power-saving response; today's and past sleep responses
- Yesterday's and the day-before activity; hourly activity; 8 blocks of 5-minute heart-rate history
- Alarm and workout summary decoders (these returned no data during testing on this watch)

Only an explicit allow-list of known read commands can be sent via `WatchClient.query`. The 1-minute heart-rate history (`0x36`) is not implemented — it returned no data on this device. No settings writes, watchface uploads, DFU, OTA, or random vendor commands are implemented.

### Protocol

`MOYOUNG-V2` packets use a five-byte header followed by an optional payload:

```text
FE EA <length-high> <length-low> <command> <payload...>
```

The total packet length includes both header and payload. The client serializes outstanding queries; protocol packets with unrelated command IDs or mismatched history indices are retained in an event queue rather than treated as the current query's response.

## Local dashboard

### 1. Start the bridge

```powershell
.\.venv\Scripts\python.exe -m bridge
```

or use `start-server.bat` / `start-server-hidden.vbs`.

- Prints the generated bridge token (persisted in `bridge/token.txt`, git-ignored).
- Binds exclusively to `127.0.0.1:8765` (`--port` to change; the extension defaults to 8765).
- Automatically scans, connects, collects device info / battery / step goal / history, and streams `FEE1`, `FEA1` and `2A37` notifications. Reconnects with backoff if the watch drops.
- Endpoints: `GET /api/status`, `/api/snapshot`, `/api/history`, `/api/diagnostics`, `POST /api/reconnect`, `POST /api/commands`, `GET /api/commands/{command_id}`, and `GET /api/telemetry/latest`. Authenticated requests use the `X-Oraimo-Token` header (or `?token=`).

### 2. Load the extension

**Option A — Load unpacked (development):**

1. Open `chrome://extensions`, enable *Developer mode*.
2. Click *Load unpacked* and select the `chrome-extension/` folder.
3. Open a new tab, click **⚙**, paste the bridge token, and save.

**Option B — Install a packed build (.crx):**

1. Grab a `Oraimo-Watch6R-Dashboard-*.crx` from the *Pack extension* workflow artifacts (or a release if you pushed a `v*` tag).
2. Open `chrome://extensions` with *Developer mode* enabled and drag the `.crx` file onto the page. (If Chrome blocks drag-installing, load the unzipped folder as in Option A — a `.zip` is provided alongside every `.crx`.)
3. Open a new tab, click **⚙**, paste the bridge token, and save.

### 3. CORS origin

The bridge's default CORS allowlist contains this exact unpacked extension origin from the original development machine:

```text
chrome-extension://dapmggaiphdbbkjegdkodemhgoceekof
```

The server validates the complete Origin value and rejects other extension IDs. If your packed/unpacked copy has a different extension ID, set it locally with `bridge/extension_origin.txt` or `--origin`:

```powershell
.\.venv\Scripts\python.exe -m bridge --origin chrome-extension://YOUR_EXTENSION_ID
```

Valid preflights get `GET, POST, OPTIONS` and the `X-Oraimo-Token`, `Content-Type`, and `Authorization` headers. The API token and Host validation are always required. After changing the bridge, stop the running process (Ctrl+C in its window, or close the hidden one) and start it again.

### Fetch Steps (on-demand reading)

The extension's **Fetch Steps** action performs a GATT read of the discovered `FEE1` activity characteristic (`0000fee1-...-00805f9b34fb`) through the current `WatchClient` — a read, not historical activity. It does not send the observed `0x59` proprietary packets. For the captured response `59 06 00 A2 04 00 47 00 00 06 00 00`, the live activity decoder returns 1,625 steps, 1,186 metres, 71 kcal.

Command flow: `POST /api/commands` with `{"type":"FETCH_STEPS"}` → poll `GET /api/commands/{command_id}` (`queued`, `running`, `succeeded`, `failed`) → results land in `GET /api/telemetry/latest`. Successful results include `steps`, `distance_meters`, `calories_kcal`, `measured_at`, and `source_characteristic`. The extension persists the last successful result and resumes polling an active command after the new-tab page is reopened. A disconnected watch and GATT failures surface as command failures; nothing is simulated.

The proprietary `FEE3` notification subscription is started only when an SDK protocol query needs it. If `FEE3` is absent on a connection, that does not block the independent `FEE1` read; the bridge records the condition once and skips the remaining proprietary settings/history queries for that connection. Protocol-backed history stays unavailable until `FEE3` is discoverable.

### Data integrity

Unavailable measurements render as "—", never as zero. FEE1 field interpretation and hourly buckets are labelled provisional. Sleep appears only when the watch actually returns records. Raw `FEE1`/`FEA1` payloads, unknown bytes and decode errors are visible in the collapsible diagnostics section. The bridge issues read-only BLE operations only.

## Packaging & CI

Two GitHub Actions workflows ship with the repo:

| Workflow | File | What it does |
| --- | --- | --- |
| **CI** | `.github/workflows/ci.yml` | Installs the SDK and runs the Python unit tests, then the Node extension API tests, on every push/PR to `main`. |
| **Pack extension** | `.github/workflows/pack.yml` | Builds `Oraimo-Watch6R-Dashboard-<ref>.crx` (CRX3) and a matching `.zip`, uploads them as workflow artifacts, and attaches them to the GitHub Release when the trigger is a `v*` tag. |

**Pack extension** runs on:

- any push to `main` touching `chrome-extension/**`,
- pushing a tag like `v1.0.0` (artifacts also land on the release),
- manual runs from the *Actions* tab (*workflow_dispatch*).

CRX3 packing uses the [`crx3`](https://www.npmjs.com/package/crx3) npm package. The output is verified in-workflow (`Cr24` magic + version check).

### Stable extension ID (recommended)

A Chrome extension's ID is derived from its signing key. To keep the same extension ID (and therefore the same CORS origin) across builds:

1. Generate a key once and keep it private — **never commit it**:

   ```powershell
   node -e "const{generateKeyPairSync}=require('crypto'),{writeFileSync}=require('fs');const{privateKey}=generateKeyPairSync('rsa',{modulusLength:2048,privateKeyEncoding:{type:'pkcs1',format:'pem'},publicKeyEncoding:{type:'spki',format:'pem'}});writeFileSync('watch6r.key.pem',privateKey)"
   ```

2. Add the file's contents as a repository secret named `CRX_PRIVATE_KEY` (*Settings → Secrets and variables → Actions*).
3. If the resulting ID differs from the default origin above, configure it via `bridge/extension_origin.txt` / `--origin`.

Without the secret, the workflow generates an ephemeral key per run and each build gets a different extension ID — fine for testing, but you'll have to re-allow the origin each time.

### Pack locally (optional)

```powershell
# .crx
npx --yes crx3 chrome-extension -o dashboard.crx -p path\to\key.pem

# .zip (PowerShell)
Compress-Archive -Path chrome-extension\* -DestinationPath dashboard.zip -Force
```

## Tests

From the project root:

```powershell
# Python SDK + bridge tests (no watch or Bluetooth needed)
python -m unittest discover -s tests -v

# Extension API error handling tests
node --test tests/extension_api.test.js
```

The Python tests cover packet construction, packet parsing, reassembly, response matching, and decoders using captured or synthetic protocol payloads. They do not test Bluetooth hardware.

## Hardware and protocol caveats

Findings from real captures on one Watch 6R; treat the *provisional* items as best-effort interpretations.

- **Activity history (confirmed):** the response to command `0x33` (51) with payload `01` or `02` is an index byte followed by three little-endian 24-bit fields — steps, distance (metres), calories — plus three extra bytes kept in `trailing_bytes`. The `jphein/moyoung-watch` decoder reads the first two fields as distance then steps; that order is wrong for the Watch 6R. A real capture for yesterday decoded to 6,801 steps (confirmed on the watch) and 4,964 m, and the day before gave the same 0.73 m per step. This is the same record layout as the live `FEE1` data.
- **Live activity (provisional):** `FEE1` captures are interpreted as three little-endian 24-bit fields — steps, distance (metres), calories — with three extra bytes stored. Based on observed captures; would benefit from further hardware confirmation.
- **Hourly buckets (unclear):** 24 little-endian 16-bit values. In the capture, only the last bucket had a value, which did not match the observed live step count. Timestamping/syncing logic is unclear.
- **Sleep stages:** values are stored and labelled per the public MoYoung reference implementation. No sleep-stage data has been captured from the Watch 6R.
- **Workout timestamps:** per the reference decoder, watch timestamps use a static UTC+8 offset. Verify against the device.
- **Move reminders and watch face:** the move reminder decodes as period (minutes), step threshold, start hour/minute, end hour/minute. A real capture, `3C 32 0A 00 16 00`, reads as every 60 minutes, 50 steps, 10:00–22:00. Inferred from one capture, not confirmed against the watch's settings screen. Bytes beyond the sixth are kept in `trailing_bytes`, as with the watch-face layout (whose trailing bytes are unknown).
- **Alarms and workout:** commands `0x21` and `0x37` returned no response during capture; the test watch had no alarms or recent workouts.
- **Power saving:** returned as raw bytes; the meaning of the two-byte payload is unknown.

## Security model

- The bridge binds to `127.0.0.1` only and validates the Host header, a per-install random token, and the exact extension Origin on every request.
- The bridge token lives in `bridge/token.txt` and is git-ignored; never commit or share it.
- The API is read-only with respect to the watch — commands perform BLE reads, and reconnects issue no BLE writes.
- The CRX signing key (`*.pem`) is git-ignored; keep it out of the repository and add it only as a GitHub Actions secret.

## License

[MIT](LICENSE)

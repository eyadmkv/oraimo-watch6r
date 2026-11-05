# TODO

Status as of 2026-11-05. Everything here is for one user (Eyad) on one machine — this is not a roadmap for a public product unless the repo goes public.

## Current features

### Python SDK (`src/oraimo_watch6r/`)

- Async read-only BLE client for the Oraimo Watch 6R (`MOYOUNG-V2` protocol) on bleak
- Scan by advertised name; manufacturer-string verification on connect
- Device info, battery, step goal, clock/units/language/dominant-hand settings
- Live activity notifications (`FEE1`), compact step notifications (`FEA1`), Heart Rate Measurement notifications (`2A37`)
- Activity history (yesterday + day before, confirmed field order), hourly activity buckets (provisional), 8 blocks of 5-minute heart-rate history
- Watch face layout, quick look, sedentary reminder, DND schedule, move reminder decoders
- Alarm and workout summary decoders (no data returned by this watch yet)
- Strict command allow-list on `WatchClient.query`; no writes, no OTA/DFU
- `on_raw` / `on_decode_error` diagnostic hooks for notification streams

### Local bridge (`bridge/`)

- Loopback-only HTTP server (`127.0.0.1:8765`) with token auth, Host validation, exact-extension-origin CORS
- Owns the single BLE connection; auto-reconnect with backoff
- Endpoints: `status`, `snapshot`, `history`, `diagnostics`, `reconnect`, `commands` (queued/running/succeeded/failed), `telemetry/latest`
- On-demand `FETCH_STEPS` GATT read via `POST /api/commands`, pollable, survives page reopen
- Records absent-`FEE3` condition once per connection without disconnecting the watch

### Chrome extension (`chrome-extension/`)

- Manifest V3 new-tab dashboard: steps vs goal, heart rate, battery, connection state, history, device info
- Unavailable data renders as "—", never zero; provisional fields labelled
- Collapsible diagnostics with raw `FEE1`/`FEA1` payloads and unknown bytes preserved
- Persists last successful reading; resumes polling an active command after reopening the tab

### Dev & CI

- 94 Python unit tests + 4 Node extension API tests, all hardware-independent, running in GitHub Actions
- `pack.yml` builds a CRX3 + zip on `chrome-extension/**` pushes, `v*` tags (attached to the GitHub Release), and manual dispatch
- v0.1.0 released with `.crx` and `.zip` assets

## Next features

### Verify on hardware (blocking)

- [ ] Run the diagnostic `examples/stream_activity.py` build on the watch and confirm whether `FEE1`/`FEA1` notifications arrive while worn (open bug: connects but shows nothing; HR stream works on the same machine)
- [ ] Confirm the live `FEE1` field interpretation (steps/distance/calories + 3 trailing bytes) against a known step count on the watch — promote or correct the provisional label
- [ ] Figure out hourly bucket timestamping/syncing (only the last bucket held a value in captures) and promote or drop the widget
- [ ] Verify workout timestamps against the device (reference decoder assumes a static UTC+8 offset)

### Protocol / SDK

- [ ] Capture and decode sleep-stage data once the watch actually returns records
- [ ] Investigate alarms (`0x21`) and workouts (`0x37`) after setting an alarm / doing a workout on the watch
- [ ] Try the 1-minute heart-rate history command (`0x36`) again with a longer history on the device
- [ ] Identify the raw power-saving response payload (2 bytes, meaning unknown)
- [ ] Identify the unknown trailing bytes on watch-face and move-reminder responses

### Bridge / extension

- [ ] Surface the "watch requires phone Bluetooth to be off" hint in the dashboard when scanning stalls
- [ ] Optional: background alarm in the extension when the bridge is down but the user is on a new tab
- [ ] Optional: battery low / goal reached notifications (local only)
- [ ] Decide whether provisional fields get promoted, relabelled, or removed once hardware-confirmed (undecided in PRODUCT.md)

### Packaging / repo

- [ ] Add `CRX_PRIVATE_KEY` repo secret for a stable extension ID across builds (one-time setup, steps in README)
- [ ] Bump `actions/checkout`, `actions/setup-node`, `actions/upload-artifact`, `softprops/action-gh-release` to Node-24-compatible versions (Node 20 deprecation warnings in CI)
- [ ] Scrub `PRODUCT.md` from git history (it was in the initial commit; only matters before the repo goes public)
- [ ] Consider a `v1.0.0` once the FEE1/FEA1 bug is closed and provisional decoders are hardware-confirmed

## Known limitations (not planned to fix)

- No writes to the watch of any kind — by design (read-only product principle)
- Phone Bluetooth must be off: the watch accepts one BLE connection at a time
- Sleep never appears unless the watch returns records; alarms/workouts show "no data on this device"
- Windows-focused: bleak's backend behaviour here is tuned to the paired Windows PC

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PiKit ICS Field Console — a deliberately small Flask app that runs on a Raspberry Pi and gives a
password-protected web UI for two things: controlling two Wi-Fi radios via NetworkManager
(`nmcli`, plus raw `iw`/`ip` for monitor mode), and driving a single RTL-SDR USB dongle (FM scan
+ RDS, ADS-B, AIS, LoRa activity detection). There is no build step, package manager, or test
suite — it's a single Flask file, one Jinja template, an install script, a systemd unit, and two
locally-compiled SDR binaries in `bin/` (not tracked in git).

## Running / developing

- Run directly: `python3 app.py` (binds `0.0.0.0:8080`, no debug mode). Requires root in practice
  because it shells out to `nmcli`/`iw`/`ip` for real network changes — on a dev machine without
  those radios/permissions, routes will mostly fail their subprocess calls.
- Auth is HTTP Basic, username `pikit`. The password comes from `PIKIT_ICS_PASSWORD` env var; if
  unset, every request gets a 503 ("password is not configured"). `PIKIT_ICS_SECRET` sets the Flask
  session secret key (defaults to the placeholder `"change-me"`).
- Production install: `sudo /home/pi/pikit-ics/install.sh`. This generates/persists the password and
  Flask secret into `/etc/pikit-ics/pikit-ics.env` (created once, not overwritten on reruns), rewires
  saved Wi-Fi profiles between the two radios, creates the `pikit-ap` hotspot connection if missing,
  installs `pikit-ics.service`, and restarts it. It does not change the currently active connection —
  a reboot is needed to apply new radio-role bindings.
- No linter/formatter/test config is present in the repo. Verify changes with `python3 -m
  py_compile app.py`, then exercise routes manually. To test without touching the live
  systemd service, run a throwaway instance on a different port:
  `PIKIT_ICS_PASSWORD=x python3 -c "import app; app.app.run(host='127.0.0.1', port=8081)"`
  (or from a separate script file — if so, `sys.path.insert(0, "/home/pi/pikit-ics")` first,
  since `python3 /abs/path/script.py` puts the *script's* directory on `sys.path`, not the cwd,
  so a bare `import app` fails). `curl -u pikit:x ...` against it. The RTL-SDR routes need real
  hardware (`rtl_power`/`rtl_fm`/`bin/redsea`/`bin/readsb`/`rtl_ais`) to do anything meaningful;
  a dev machine without the dongle will just get subprocess failures. When background-testing a
  Python server across several iterations, track it by exact PID (`ss -tlnp | grep <port>`) —
  `pkill -f "port=NNNN"` will never match, since the port number lives inside the script's source,
  not the process's argv, so it silently kills nothing and leaves zombies serving stale code.

## Architecture: the two-radio split

The entire design centers on keeping two Wi-Fi interfaces in strictly separate roles so that field
work can never accidentally kill the management connection:

- **`wlan0` (`MANAGEMENT`)** — the safe return path. Auto-joins the saved `FFLF-ZONE` profile
  (priority 100) if present; falls back to the self-hosted `pikit-ap` hotspot at `192.168.4.1`
  (priority -100, `ipv4.never-default yes`) otherwise. The console can save additional management
  profiles, but only ones explicitly connected through `/management/connect`.
- **`wlan1` (`FIELD`)** — starts disconnected after every boot. Profiles saved here (via
  `/field/connect`) always have `connection.autoconnect no`, so a previously visited boat/ICS network
  is never silently rejoined. This radio can also be flipped out of NetworkManager entirely into
  802.11 monitor mode (`/field/monitor`) for wireless analysis, and back (`/field/managed`).

The hotspot profile (`pikit-ap`) is bound by `connection.interface-name`, never a MAC address, so
role assignment survives Wi-Fi hardware swaps. `install.sh` enforces the wlan0/wlan1 split for
*existing* saved profiles at install time (based on SSID `FFLF-ZONE` or current `interface-name`);
`app.py`'s `save_wifi_profile()` enforces it going forward for anything saved through the UI, by
always passing an explicit `connection.interface-name`.

## Architecture: RTL-SDR (one dongle, four tools, mutually exclusive)

There is exactly one RTL-SDR dongle, so exactly one of these can hold it at a time.
`ACTIVE_SDR_SESSION` (a module-level global, like `LAST_FM_SCAN`) is the single source of
truth: `None` when free, else `{"mode": "adsb"|"ais"|"lora", "process": Popen, ...}`.
`dongle_busy_redirect(anchor)` is the shared guard every start-a-session route calls first;
it refuses and flashes an error if a session is already active. Stopping a session terminates
its `Popen` and sets the global back to `None`.

- **FM scan** (`/sdr/fm-scan`) — one-shot, not a session: runs `rtl_power` once (~1-2s),
  parses the CSV into `LAST_FM_SCAN` (samples + peaks), and returns immediately. `build_fm_chart()`
  recomputes SVG geometry (paths, ticks, peak markers, a hover-lookup array) fresh on every
  page load from that stored raw data, decoupling geometry/layout constants from the scan itself.
  **Identify** (`/sdr/fm-identify`) is a bounded ~15s foreground listen (`rtl_fm | bin/redsea`,
  two `Popen`s piped together, killed by a `threading.Timer`) that fills in one peak's RDS name
  in place — also not a session, since it's short and synchronous.
- **ADS-B / AIS / LoRa** (`/sdr/{adsb,ais,lora}/{start,stop}`) — long-running sessions. Each
  `start_*_tracking()` spawns a `Popen` (plus, for AIS/LoRa, a daemon `threading.Thread` pumping
  its stdout/stderr line-by-line into module-level state protected by a `Lock`). ADS-B is the
  odd one out: `readsb` writes `aircraft.json` to disk every second on its own, so
  `read_adsb_aircraft()` just reads that file fresh each page load — no thread needed.
- The page auto-refreshes every 3s (`<meta http-equiv="refresh">`, conditional on
  `active_sdr_mode`) so a running session's table updates without manual reloads.
- **AIS decoding is hand-written** (`_decode_ais_line` → `_apply_ais_message`, plus the 6-bit
  ASCII armor/charset helpers): no packaged Python AIVDM/NMEA library exists. It reassembles
  multi-part sentences by sequence id and decodes position (types 1/2/3) and name (type 5).
- **LoRa is a presence/activity finder, not a decoder** — real LoRa PHY decoding needs GNU Radio
  + chirp dechirping, out of scope. `_process_lora_line` instead: (1) tracks a per-1kHz-bin EMA
  baseline (`LORA_BASELINE`) rather than comparing to each pass's own median, because the
  dongle's per-hop filter shape otherwise reads as a constant "signal"; (2) only counts a
  *contiguous run* of `LORA_MIN_RUN_BINS`+ elevated bins as a candidate (matching real
  125-500kHz LoRa channel widths — a lone spiking bin is far more likely to be noise);
  (3) logs only the rising edge (`LORA_ACTIVE`), not every pass a signal stays up; (4) auto-squelches
  (`LORA_SQUELCHED`) any bin re-triggering more than `LORA_SQUELCH_TRIGGER_COUNT` times in
  `LORA_SQUELCH_WINDOW_SEC` — a real LoRaWAN device transmits at most every few minutes, so
  frequent re-triggers mean chronic local interference, not traffic. Even with all four
  safeguards, this Pi's 915-928MHz noise floor is broadband-elevated (almost certainly RTL-SDR +
  USB3 self-interference — a documented combination), so expect it to still report more often
  than genuine sporadic LoRa traffic would; the real fix is physical (USB2 port/hub, shorter/
  shielded cable, ferrite choke), not further tuning the threshold.

### The two locally-built binaries (`bin/`, gitignored)

Debian's packages don't cover these: `redsea` isn't packaged for Debian at all, and Debian's
`readsb` package is compiled *without* RTL-SDR support (Beast/serial inputs only) — which is
also why a stock Raspberry Pi OS image's pre-installed `readsb.service` may be crash-looping
uselessly (`journalctl -u readsb` shows `ERROR: Unknown device type:0`, restart counter climbing
every ~15s). Rebuild instructions and apt dependencies are in `README.md`. `rtl-ais`, by
contrast, *is* a normal Debian package (`apt install rtl-ais`) — no build needed.

## Code structure (`app.py`)

- Thin NetworkManager wrapper functions (`status`, `scan`, `connection_names`,
  `wireless_profiles`, `wireless_mode`) all shell out to `nmcli`/`iw` via the `run()` helper
  (`subprocess.run` with `text=True, capture_output=True, check=False`) and parse `-t` (terse,
  colon-delimited) output. Nothing here mutates state.
- `save_wifi_profile()` is the one path that creates/modifies a wifi connection profile; it always
  pins `connection.interface-name` to the given radio and derives `wifi-sec.key-mgmt` from the
  scanned security string via `wifi_key_mgmt()`. It rejects enterprise (802.1x/EAP) networks and
  requires a password for anything not open. Connection names are deterministic:
  `f"{interface}-{ssid}"` (see `wifi_connection_name`), so reconnecting to the same SSID on the same
  radio updates the existing profile (`connection modify`) instead of creating duplicates.
- All user-supplied names (SSID, saved profile name) are validated with `valid_name()` /
  `SAFE_NAME` — printable, no control characters, ≤96 chars — before being interpolated into any
  `nmcli` argument. Preserve this validation on any new route that takes a name into a subprocess
  call; args are passed as a list to `subprocess.run` (no shell), so injection risk is specifically
  about NetworkManager accepting malformed profile names, not shell metacharacters.
- Routes are grouped by radio prefix: `/management/*` (ap, connect, profile) and `/field/*` (connect,
  disconnect, monitor, managed). Every route is decorated with `@require_auth` and ends with a
  `flash(...)` + `redirect(url_for("index"))` — there is no JSON API, only the single-page UI at `/`.
- `field_monitor()` / `field_managed()` are ordered sequences of external commands (`nmcli device
  set managed no` → `ip link down` → `iw ... set type monitor` → `ip link up`, and the reverse) where
  each step's failure short-circuits with a flash error. Keep new multi-step radio-mode changes in
  this same "run steps, bail on first failure" shape.

## Template (`templates/index.html`)

Single file, inline `<style>`, no build pipeline, no JS framework. Three top-level tabs (WiFi,
RTL-SDR, Docs) live in one outer `.tab-group`; RTL-SDR contains its own nested `.tab-group` for
its four sub-tabs (FM scan, ADS-B, AIS, LoRa). The tab-toggle script is generic and
group-scoped (`:scope > [role="tablist"] [role="tab"]`) so nesting doesn't cross-wire — adding
another level of sub-tabs needs no script changes, just another `.tab-group` wrapper.

Every POST route redirects back with `_anchor="<tab-id>"` (e.g. `#adsb-tab`) so the right tab
survives the full-page reload — tab selection is pure client-side JS state, lost on any normal
redirect otherwise. `revealTab()` in the bottom `<script>` block handles restoring it: given a
hash, it walks *up* through `tab.closest('.tab-panel')` recursively, clicking each ancestor
tab-group's owning tab before the target itself, so a nested sub-tab (e.g. `#lora-tab`, two
levels deep) gets revealed correctly regardless of nesting depth. When adding a new sub-tab,
anchor its routes at that sub-tab's own id, not the parent's — anchoring everything at the
parent tab is the bug this fixes (redirects all land on the sub-tab's hardcoded default instead
of wherever the user actually was).

WiFi is the actual controls, split into `.session.management` / `.session.field` sections
mirroring the `wlan0`/`wlan1` split in `app.py`. Docs is static in-app help text — keep it in
sync when adding routes/behavior, it's the operator's only reference in the field with no
internet. When adding a route, thread its result through `index()`'s render context and add a
corresponding form/card here — there's no partial templating or JSON API.

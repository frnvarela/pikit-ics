# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PiKit ICS Field Console — a deliberately small Flask app that runs on a Raspberry Pi and gives a
password-protected web UI for controlling two Wi-Fi radios via NetworkManager (`nmcli`), plus raw
`iw`/`ip` for monitor mode. There is no build step, package manager, or test suite — it's a single
Flask file, one Jinja template, an install script, and a systemd unit.

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
- No linter/formatter/test config is present in the repo; there's nothing to run beyond executing
  the script and exercising routes manually (or via `curl -u pikit:<password> ...`).

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

Single file, inline `<style>`, no build pipeline, no JS framework — a couple of inline `<script>`-free
tab buttons toggle `hidden` via minimal vanilla JS elsewhere in the file. Two tabs: WiFi (the actual
controls, split into `.session.management` / `.session.field` sections mirroring the `wlan0`/`wlan1`
split in `app.py`) and Docs (static in-app help text). When adding a route, thread its result through
`index()`'s render context and add a corresponding form/card here — there's no partial templating.

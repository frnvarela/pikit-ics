# PiKit ICS Field Console

`pikit-ics` keeps the two Wi-Fi radios in distinct roles:

- `wlan0` — management: automatically joins the saved `FFLF-ZONE` network; if
  that network is unavailable, it starts the PiKit hotspot (`192.168.4.1`).
  The console can also save and select another management Wi-Fi profile on
  `wlan0` when the deployment changes.
- `wlan1` — field/test: starts disconnected after boot. Equipment connections
  made from the console are pinned to `wlan1` and set not to auto-connect, so a
  previously visited boat or ICS network is never silently rejoined.

Install it once with `sudo /home/pi/pikit-ics/install.sh`. The installer prints a generated console password and, on a fresh Pi, creates the `PiKit-ICS` fallback hotspot with its own generated passphrase. Open `http://<current-Pi-IP>:8080/` and log in as `pikit`.

At boot, `FFLF-ZONE` and any management profile explicitly saved through the
console are bound to `wlan0` and have priority 100. If none is available,
NetworkManager activates the PiKit hotspot at priority -100. The installer binds
all other saved Wi-Fi profiles to `wlan1` and disables their autoconnect.

The hotspot profile is bound by interface name, not a Wi-Fi MAC address, so it
continues to work if the Pi's wireless hardware is replaced.

The web service runs as root only because NetworkManager and monitor-mode operations require it. It accepts a generated password and only runs fixed, validated network commands; it does not display saved Wi-Fi secrets.

## RTL-SDR

A third console tab drives the RTL-SDR USB dongle. There is only **one** dongle, so
only one of the four tools below can run at a time — starting one while another is
active is refused until you stop it (this is enforced server-side, not just in the UI).

- **FM scan** — one-shot `rtl_power` sweep of 88–108 MHz, rendered as a spectrum chart
  with the strongest stations marked. **Identify** on a station listens live for up to
  15 seconds and decodes its RDS name via a self-built `redsea` (not in Debian's repos;
  built from source into `bin/redsea`, see below).
- **ADS-B** — Start/Stop toggle. Runs a self-built RTL-SDR-enabled `readsb`
  (`bin/readsb`) writing `aircraft.json` every second; the console reads that file on
  each page load. Lists flight, altitude, speed, track, position.
- **AIS** — Start/Stop toggle. Runs the packaged `rtl-ais` (`apt install rtl-ais`),
  decoding its NMEA (AIVDM) stream with a small hand-written 6-bit-armor/position/name
  parser in `app.py` (no packaged Python AIS library exists). Lists vessel name, MMSI,
  speed, course, position — the name only appears once its separate static-data message
  arrives, which can take longer than position reports.
- **LoRa finder** — Start/Stop toggle. Continuously sweeps 915–928 MHz with `rtl_power`
  and flags energy bursts against each frequency bin's own adaptive historical baseline
  (not the pass's median, which is skewed by the dongle's per-hop filter shape). It's a
  **presence detector, not a packet decoder** — no device IDs or payloads, just that
  something transmitted, where, and roughly how wide/strong. It requires several
  adjacent bins to rise together (matching real 125–500 kHz LoRa channel widths) and
  auto-mutes any frequency re-triggering more than a real device would ever transmit.
  **This band is prone to broadband self-interference from the Pi's own USB3 bus** —
  if results stay persistently busy even with those safeguards, that's the likely cause;
  moving the dongle to a USB2 port/hub, a shorter/shielded USB cable, or a ferrite choke
  typically clears it up.

## GNSS

A fourth console tab reads position/heading data from a GNSS receiver connected over
USB-serial (built for the Unicore UM982 dual-antenna RTK module, but any NMEA-0183
receiver works for position). It depends on the packaged `python3-serial`
(`apt install python3-serial`), installed automatically by `install.sh`.

### Why two binaries are built from source

`bin/redsea` and `bin/readsb` are compiled locally (not tracked in git — see
`.gitignore`) because neither Debian package works for this purpose out of the box:
`redsea` isn't packaged for Debian at all, and Debian's `readsb` package is built
*without* RTL-SDR support (only Beast/GNS-HULC serial inputs), which is also why a
stock Raspberry Pi OS image's pre-installed `readsb.service` may be crash-looping
uselessly (ours was, for hours, before we disabled it — check
`systemctl status readsb.service` if re-provisioning). To rebuild either from scratch:

```
git clone https://github.com/windytan/redsea.git && cd redsea
meson setup build && ninja -C build
cp build/redsea /home/pi/pikit-ics/bin/redsea

git clone https://github.com/wiedehopf/readsb.git && cd readsb
make RTLSDR=yes
cp readsb /home/pi/pikit-ics/bin/readsb
```

Both need `libncurses-dev libzstd-dev libusb-1.0-0-dev librtlsdr-dev` (readsb) and
`libsndfile1-dev libliquid-dev` (redsea) installed via `apt`.

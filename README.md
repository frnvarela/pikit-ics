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

#!/bin/bash
set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
  echo "Run with: sudo $0"
  exit 1
fi

install -d -m 700 /etc/pikit-ics
if [ ! -f /etc/pikit-ics/pikit-ics.env ]; then
  password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  umask 077
  printf 'PIKIT_ICS_PASSWORD=%s\nPIKIT_ICS_SECRET=%s\n' "$password" "$secret" > /etc/pikit-ics/pikit-ics.env
  echo "PiKit ICS login: pikit"
  echo "PiKit ICS password: $password"
  echo "Store this password now; it is kept in /etc/pikit-ics/pikit-ics.env."
fi

# wlan0 is the management radio.  FFLF-ZONE and any profile deliberately saved
# for wlan0 in the console remain management profiles; all other saved Wi-Fi
# profiles are field-only and must not reconnect on wlan1 after a reboot.
fflf_profile=""
while IFS=: read -r profile profile_type; do
  [ "$profile_type" = "802-11-wireless" ] || continue
  [ "$profile" = "pikit-ap" ] && continue
  ssid="$(nmcli -g 802-11-wireless.ssid connection show "$profile" 2>/dev/null || true)"
  assigned_interface="$(nmcli -g connection.interface-name connection show "$profile" 2>/dev/null || true)"
  if [ "$ssid" = "FFLF-ZONE" ] || [ "$assigned_interface" = "wlan0" ]; then
    [ "$ssid" = "FFLF-ZONE" ] && fflf_profile="$profile"
    nmcli connection modify "$profile" \
      connection.interface-name wlan0 \
      connection.autoconnect yes \
      connection.autoconnect-priority 100
  else
    nmcli connection modify "$profile" \
      connection.interface-name wlan1 \
      connection.autoconnect no
  fi
done < <(nmcli -t -f NAME,TYPE connection show)

if [ -z "$fflf_profile" ]; then
  echo "FFLF-ZONE is not a saved Wi-Fi profile yet; wlan0 will use the PiKit-ICS hotspot."
  echo "Save FFLF-ZONE once on wlan0, then rerun this installer to enable automatic joining."
fi

# Make the management fallback self-contained on a fresh Pi.  Existing PiKit
# installations retain their current hotspot SSID and passphrase.
if ! nmcli -g NAME connection show | grep -Fxq "pikit-ap"; then
  ap_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  nmcli connection add type wifi ifname wlan0 con-name pikit-ap ssid PiKit-ICS \
    802-11-wireless.mode ap wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$ap_password" \
    ipv4.method shared ipv6.method disabled
  echo "Created fallback hotspot: PiKit-ICS"
  echo "Hotspot password: $ap_password"
  echo "Store this password now; it is kept by NetworkManager."
fi

# wlan0 is deliberately the management radio. A saved Wi-Fi profile (priority 0)
# wins when available; the PiKit AP (priority -100) is the fallback.  Do not pin
# this profile to a MAC address: interface-name is the stable role boundary.
nmcli connection modify pikit-ap \
  connection.interface-name wlan0 \
  802-11-wireless.mac-address "" \
  connection.autoconnect yes \
  connection.autoconnect-priority -100 \
  ipv4.method shared ipv4.addresses 192.168.4.1/24 ipv4.never-default yes \
  ipv6.method disabled

install -m 644 /home/pi/pikit-ics/pikit-ics.service /etc/systemd/system/pikit-ics.service
systemctl daemon-reload
systemctl enable pikit-ics.service
systemctl restart pikit-ics.service
echo "PiKit ICS is running on port 8080. It will be reachable at http://<Pi-IP>:8080/"
echo "The active Wi-Fi connection was not changed. Restart the Pi to apply the radio roles."

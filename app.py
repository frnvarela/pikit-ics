#!/usr/bin/env python3
"""PiKit ICS Field Console — a deliberately small NetworkManager control UI."""

from __future__ import annotations

import functools
import os
import re
import subprocess
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("PIKIT_ICS_SECRET", "change-me")
PASSWORD = os.environ.get("PIKIT_ICS_PASSWORD", "")
MANAGEMENT = "wlan0"
FIELD = "wlan1"
MANAGEMENT_SSID = "FFLF-ZONE"
SAFE_NAME = re.compile(r"^[^\x00-\x1f]{1,96}$")


def run(*args: str, timeout: int = 25) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


def output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or result.stderr or "Command completed.").strip()


def require_auth(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not PASSWORD:
            return "PiKit ICS password is not configured. Run install.sh as root.", 503
        auth = request.authorization
        if not auth or auth.username != "pikit" or auth.password != PASSWORD:
            return ("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="PiKit ICS"'})
        return view(*args, **kwargs)

    return wrapped


def status() -> list[dict[str, str]]:
    result = run("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status")
    rows = []
    for line in result.stdout.splitlines():
        values = line.split(":", 3)
        if len(values) == 4 and values[0] in {MANAGEMENT, FIELD}:
            rows.append(dict(zip(("device", "type", "state", "connection"), values)))
    return rows


def scan(interface: str) -> list[dict[str, str]]:
    result = run("nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", interface)
    networks: dict[str, dict[str, str]] = {}
    for line in result.stdout.splitlines():
        values = line.split(":", 2)
        if len(values) == 3 and values[0]:
            networks.setdefault(values[0], {"ssid": values[0], "signal": values[1], "security": values[2] or "open"})
    return list(networks.values())[:30]


def connection_names() -> set[str]:
    result = run("nmcli", "-t", "-f", "NAME", "connection", "show")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def wireless_profiles(interface: str) -> list[dict[str, str]]:
    """Return saved Wi-Fi profiles intentionally assigned to one radio."""
    result = run("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show")
    profiles = []
    for line in result.stdout.splitlines():
        name, separator, kind = line.partition(":")
        if not separator or kind != "802-11-wireless" or not valid_name(name):
            continue
        assigned = run("nmcli", "-g", "connection.interface-name", "connection", "show", name)
        if assigned.stdout.strip() != interface:
            continue
        ssid = run("nmcli", "-g", "802-11-wireless.ssid", "connection", "show", name)
        profiles.append({"name": name, "ssid": ssid.stdout.strip() or name})
    return profiles


def wireless_mode(interface: str, radio: dict[str, str] | None) -> str:
    """Describe a radio's usable mode, including monitor mode outside NM."""
    details = run("iw", "dev", interface, "info")
    if re.search(r"^\s*type\s+monitor\s*$", details.stdout, re.MULTILINE):
        return "monitor"
    if not radio or radio["state"] == "unavailable":
        return "unavailable"
    if radio["state"] == "connected":
        return "connected"
    return "managed"


def valid_name(value: str) -> bool:
    return bool(SAFE_NAME.fullmatch(value))


def wifi_connection_name(interface: str, ssid: str) -> str:
    name = f"{interface}-{ssid}".strip("-")
    return name[:96]


def wifi_key_mgmt(security: str, password: str) -> str:
    normalized = (security or "").strip().lower()
    if not normalized or normalized in {"--", "none", "open"}:
        return "none"
    if "802.1x" in normalized or "eap" in normalized:
        return "enterprise"
    if "sae" in normalized or "wpa3" in normalized:
        return "sae"
    if password:
        return "wpa-psk"
    return "wpa-psk"


def save_wifi_profile(interface: str, ssid: str, password: str, security: str, autoconnect: bool, priority: int) -> subprocess.CompletedProcess[str]:
    con_name = wifi_connection_name(interface, ssid)
    key_mgmt = wifi_key_mgmt(security, password)
    if key_mgmt == "enterprise":
        return subprocess.CompletedProcess(("nmcli",), 1, "", "Enterprise Wi-Fi is not supported from this console.")
    if key_mgmt != "none" and not password:
        return subprocess.CompletedProcess(("nmcli",), 1, "", "Password is required for this secured Wi-Fi network.")
    existing = con_name in connection_names()
    if existing:
        args = ["nmcli", "connection", "modify", con_name]
    else:
        args = ["nmcli", "connection", "add", "type", "wifi", "con-name", con_name, "ifname", interface, "ssid", ssid]
    args.extend((
        "connection.interface-name", interface,
        "connection.autoconnect", "yes" if autoconnect else "no",
        "connection.autoconnect-priority", str(priority),
        "ipv4.method", "auto",
        "ipv6.method", "auto",
        "wifi-sec.key-mgmt", key_mgmt,
    ))
    if key_mgmt == "none" and existing:
        args.append("-wifi-sec.psk")
    elif key_mgmt != "none":
        args.extend(("wifi-sec.psk", password))
    return run(*args, timeout=45)


@app.get("/")
@require_auth
def index():
    radios = {radio["device"]: radio for radio in status()}
    return render_template(
        "index.html",
        radios=radios,
        management_mode=wireless_mode(MANAGEMENT, radios.get(MANAGEMENT)),
        field_mode=wireless_mode(FIELD, radios.get(FIELD)),
        management_networks=scan(MANAGEMENT),
        management_profiles=wireless_profiles(MANAGEMENT),
        field_networks=scan(FIELD),
    )


@app.post("/management/ap")
@require_auth
def management_ap():
    result = run("nmcli", "connection", "up", "pikit-ap", "ifname", MANAGEMENT, timeout=45)
    flash(output(result), "ok" if result.returncode == 0 else "error")
    return redirect(url_for("index"))


@app.post("/management/connect")
@require_auth
def management_connect():
    """Join a management network and make it available after a reboot."""
    ssid = request.form.get("ssid", MANAGEMENT_SSID).strip()
    password = request.form.get("password", "")
    security = request.form.get("security", "").strip()
    if not valid_name(ssid):
        flash("Enter a valid network name.", "error")
        return redirect(url_for("index"))
    result = save_wifi_profile(MANAGEMENT, ssid, password, security, True, 100)
    if result.returncode == 0:
        run("nmcli", "connection", "up", wifi_connection_name(MANAGEMENT, ssid), "ifname", MANAGEMENT, timeout=45)
    flash(output(result), "ok" if result.returncode == 0 else "error")
    return redirect(url_for("index"))


@app.post("/management/profile")
@require_auth
def management_profile():
    """Activate one of the saved profiles assigned to wlan0."""
    profile = request.form.get("profile", "").strip()
    available = {item["name"] for item in wireless_profiles(MANAGEMENT)}
    if profile not in available:
        flash("Choose a saved wlan0 Wi-Fi profile.", "error")
        return redirect(url_for("index"))
    result = run("nmcli", "connection", "up", profile, "ifname", MANAGEMENT, timeout=45)
    flash(output(result), "ok" if result.returncode == 0 else "error")
    return redirect(url_for("index"))


@app.post("/field/connect")
@require_auth
def field_connect():
    ssid = request.form.get("ssid", "").strip()
    password = request.form.get("password", "")
    security = request.form.get("security", "").strip()
    if not valid_name(ssid):
        flash("Enter a valid network name.", "error")
        return redirect(url_for("index"))
    run("nmcli", "device", "set", FIELD, "managed", "yes")
    result = save_wifi_profile(FIELD, ssid, password, security, False, 0)
    if result.returncode == 0:
        run("nmcli", "connection", "up", wifi_connection_name(FIELD, ssid), "ifname", FIELD, timeout=45)
    flash(output(result), "ok" if result.returncode == 0 else "error")
    return redirect(url_for("index"))


@app.post("/field/disconnect")
@require_auth
def field_disconnect():
    result = run("nmcli", "device", "disconnect", FIELD)
    flash(output(result), "ok" if result.returncode == 0 else "error")
    return redirect(url_for("index"))


@app.post("/field/monitor")
@require_auth
def field_monitor():
    # NetworkManager reports an error when asked to disconnect an interface
    # that is already disconnected.  That is the desired starting state for
    # monitor mode, so only surface other disconnect failures.
    disconnect = run("nmcli", "device", "disconnect", FIELD)
    if disconnect.returncode and "not active" not in output(disconnect).lower():
        flash(output(disconnect), "error")
        return redirect(url_for("index"))
    steps = [
        ("nmcli", "device", "set", FIELD, "managed", "no"),
        ("ip", "link", "set", FIELD, "down"),
        ("iw", "dev", FIELD, "set", "type", "monitor"),
        ("ip", "link", "set", FIELD, "up"),
    ]
    for step in steps:
        result = run(*step)
        if result.returncode:
            flash(output(result), "error")
            return redirect(url_for("index"))
    flash("wlan1 is now in monitor mode. It is intentionally disconnected from NetworkManager.", "ok")
    return redirect(url_for("index"))


@app.post("/field/managed")
@require_auth
def field_managed():
    steps = [
        ("ip", "link", "set", FIELD, "down"),
        ("iw", "dev", FIELD, "set", "type", "managed"),
        ("ip", "link", "set", FIELD, "up"),
        ("nmcli", "device", "set", FIELD, "managed", "yes"),
    ]
    for step in steps:
        result = run(*step)
        if result.returncode:
            flash(output(result), "error")
            return redirect(url_for("index"))
    flash("wlan1 is back in managed mode and ready to join field equipment Wi-Fi.", "ok")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)

#!/usr/bin/env python3
"""PiKit ICS Field Console — a deliberately small NetworkManager control UI."""

from __future__ import annotations

import functools
import glob
import json
import math
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import serial
from flask import Flask, flash, redirect, render_template, request, url_for

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("PIKIT_ICS_SECRET", "change-me")
PASSWORD = os.environ.get("PIKIT_ICS_PASSWORD", "")
MANAGEMENT = "wlan0"
FIELD = "wlan1"
MANAGEMENT_SSID = "FFLF-ZONE"
SAFE_NAME = re.compile(r"^[^\x00-\x1f]{1,96}$")
FM_RANGE = "88M:108M:12.5k"
CHART_SIZE = {"width": 880, "height": 220}
CHART_MARGIN = {"left": 36, "right": 10, "top": 14, "bottom": 26}
LAST_FM_SCAN: dict[str, object] | None = None
REDSEA_BIN = str(Path(__file__).parent / "bin" / "redsea")
RDS_SAMPLE_RATE = "171000"
RDS_LISTEN_SECONDS = 15
READSB_BIN = str(Path(__file__).parent / "bin" / "readsb")
ADSB_JSON_DIR = Path("/run/pikit-ics-adsb")
ACTIVE_SDR_SESSION: dict[str, object] | None = None
AIS_VESSELS: dict[int, dict[str, object]] = {}
AIS_LOCK = threading.Lock()
AIS_CHARSET = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
LORA_RANGE = "915M:928M:25k"
LORA_BURST_THRESHOLD_DB = 8.0
LORA_MAX_SIGHTINGS = 100
LORA_QUIET_ALPHA = 0.2  # baseline adapts quickly to a bin's normal (quiet) level
LORA_BUSY_ALPHA = 0.02  # ...but barely moves while a bin is mid-burst, so it can't "learn" the burst as normal
LORA_SQUELCH_WINDOW_SEC = 30  # a real LoRaWAN device transmits at most every few minutes...
LORA_SQUELCH_TRIGGER_COUNT = 3  # ...so >3 rising edges in this window means chronic local noise, not traffic
LORA_MIN_RUN_BINS = 4  # ~100kHz+ at our 25kHz step; real chirps (125/250/500kHz) light up several
# bins together, unlike an isolated broadband-noise spike in a single bin
LORA_SIGHTINGS: list[dict[str, object]] = []
LORA_BASELINE: dict[int, float] = {}  # bin key (freq rounded to 1 kHz) -> EMA noise floor in dB
LORA_ACTIVE: set[int] = set()  # bins currently above threshold, so only the rising edge gets logged
LORA_TRIGGER_LOG: dict[int, list[float]] = {}  # bin key -> recent rising-edge timestamps
LORA_SQUELCHED: set[int] = set()  # bins auto-muted as chronic interference, not real bursts
LORA_LOCK = threading.Lock()

GNSS_PORT_GLOBS = ("/dev/ttyUSB*", "/dev/ttyACM*")
GNSS_DEFAULT_BAUD = 115200
# Tried in this order against every detected port: the UM982's own default first,
# then other rates common on GNSS/serial gear. Each combination gets one bounded
# read, so keep this list short — it's multiplied by port count on every /gnss/detect.
GNSS_PROBE_BAUDS = (115200, 230400, 9600, 38400, 57600, 460800)
GNSS_FIX_QUALITY = {
    "0": "No fix", "1": "GPS", "2": "DGPS", "3": "PPS",
    "4": "RTK fixed", "5": "RTK float", "6": "Estimated",
}
# UNIHEADINGA pos-type/sol-status enums, Unicore Reference Commands Manual for N4
# High Precision Products, Table 0-4 / Table 0-5 — only entries relevant to a
# UM982 dual-antenna heading solution, unlisted values fall back to the raw string.
GNSS_HEADING_POS_TYPE = {
    "NONE": "No solution", "NARROW_INT": "RTK fixed", "WIDE_INT": "RTK wide-lane fixed",
    "NARROW_FLOAT": "RTK narrow-lane float", "L1_FLOAT": "L1 float", "L1_INT": "L1 fixed",
    "INS": "INS-derived", "INS_RTKFLOAT": "INS + RTK float", "INS_RTKFIXED": "INS + RTK fixed",
}
GNSS_HEADING_SOL_STATUS = {
    "SOL_COMPUTED": "Solution computed", "INSUFFICIENT_OBS": "Insufficient observations",
    "NO_CONVERGENCE": "No convergence", "COV_TRACE": "Covariance exceeds maximum",
}
GNSS_SESSION: dict[str, object] | None = None  # {"serial": Serial, "thread": Thread}
GNSS_FIX: dict[str, object] = {}
GNSS_LOCK = threading.Lock()
GNSS_DETECTED: dict[str, object] | None = None  # {"port": str, "baud": int}, last successful auto-detect
GNSS_EVENTS_MAX = 200          # milestones are rare — this spans days of field use
GNSS_TELEMETRY_MAX = 500       # ~1 row/sec -> last ~8min, kept across start/stop cycles
GNSS_TELEMETRY_MIN_INTERVAL = 1.0  # throttle: at most one telemetry row per second
GNSS_EVENTS: list[dict[str, object]] = []       # newest-first: {"ts", "time", "message"}
GNSS_TELEMETRY: list[dict[str, object]] = []    # newest-first: a GNSS_FIX snapshot per tick
GNSS_LAST_FIX_QUALITY: str | None = None       # baseline for detecting a fix-quality transition
GNSS_LAST_HEADING_POS_TYPE: str | None = None  # baseline for detecting a heading transition
GNSS_LAST_TELEMETRY_TS = 0.0


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


def parse_power_csv(text: str) -> list[tuple[float, float]]:
    """Flatten rtl_power's chunked CSV rows into sorted (freq_hz, db) samples."""
    samples: list[tuple[float, float]] = []
    for line in text.splitlines():
        values = line.split(",")
        if len(values) < 7:
            continue
        low, step = float(values[2]), float(values[4])
        samples.extend((low + i * step, float(db)) for i, db in enumerate(values[6:]))
    samples.sort(key=lambda item: item[0])
    return samples


def find_fm_peaks(
    samples: list[tuple[float, float]],
    min_separation_hz: float = 200_000,
    threshold_db: float = 6.0,
    max_peaks: int = 12,
) -> list[dict[str, float]]:
    """Pick the strongest bins clear of the noise floor, one per station."""
    noise_floor = sorted(db for _, db in samples)[len(samples) // 2]
    candidates = sorted(
        (item for item in samples if item[1] - noise_floor >= threshold_db),
        key=lambda item: item[1],
        reverse=True,
    )
    peaks: list[dict[str, float]] = []
    for freq, db in candidates:
        if any(abs(freq - peak["freq_hz"]) < min_separation_hz for peak in peaks):
            continue
        peaks.append({"freq_hz": freq, "freq_mhz": round(freq / 1e6, 1), "db": round(db, 1), "name": None})
        if len(peaks) >= max_peaks:
            break
    peaks.sort(key=lambda peak: peak["freq_hz"])
    return peaks


def run_fm_scan() -> dict[str, object]:
    """Run a single-shot rtl_power sweep of the FM broadcast band."""
    result = run("rtl_power", "-f", FM_RANGE, "-i", "1", "-1", timeout=45)
    if result.returncode != 0:
        return {"error": output(result) or "rtl_power failed. Is the RTL-SDR dongle connected?"}
    samples = parse_power_csv(result.stdout)
    if not samples:
        return {"error": "rtl_power returned no data."}
    return {"samples": samples, "peaks": find_fm_peaks(samples)}


def identify_fm_station(freq_hz: float) -> dict[str, str]:
    """Live-decode a station's RDS PS (station name) by listening for a few seconds."""
    try:
        tuner = subprocess.Popen(
            ("rtl_fm", "-f", f"{freq_hz:.0f}", "-M", "fm", "-l", "0", "-A", "std", "-s", RDS_SAMPLE_RATE, "-g", "20"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        decoder = subprocess.Popen(
            (REDSEA_BIN, "-r", RDS_SAMPLE_RATE),
            stdin=tuner.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError as error:
        return {"error": str(error)}
    tuner.stdout.close()

    timer = threading.Timer(RDS_LISTEN_SECONDS, lambda: (tuner.terminate(), decoder.terminate()))
    timer.start()
    name = None
    for line in decoder.stdout:
        try:
            group = json.loads(line)
        except ValueError:
            continue
        if group.get("ps"):
            name = group["ps"].strip()
    timer.cancel()
    tuner.wait(timeout=5)
    decoder.wait(timeout=5)

    if name:
        return {"name": name}
    return {"error": f"No station name decoded within {RDS_LISTEN_SECONDS}s. The signal may be weak or not carry RDS."}


def build_fm_chart(scan_result: dict[str, object]) -> dict[str, object]:
    """Turn raw scan samples into ready-to-render SVG geometry (paths, ticks, peak markers)."""
    samples: list[tuple[float, float]] = scan_result["samples"]
    low_hz, high_hz = samples[0][0], samples[-1][0]
    dbs = [db for _, db in samples]
    min_db, max_db = min(dbs), max(dbs)
    pad = max((max_db - min_db) * 0.08, 1.0)
    min_db, max_db = min_db - pad, max_db + pad

    left, top = CHART_MARGIN["left"], CHART_MARGIN["top"]
    plot_w = CHART_SIZE["width"] - left - CHART_MARGIN["right"]
    plot_h = CHART_SIZE["height"] - top - CHART_MARGIN["bottom"]
    baseline_y = top + plot_h

    def x_of(freq: float) -> float:
        return left + (freq - low_hz) / (high_hz - low_hz) * plot_w

    def y_of(db: float) -> float:
        return top + (1 - (db - min_db) / (max_db - min_db)) * plot_h

    buckets: list[float | None] = [None] * (int(plot_w) + 1)
    for freq, db in samples:
        col = min(max(int(x_of(freq)) - left, 0), int(plot_w))
        if buckets[col] is None or db > buckets[col]:
            buckets[col] = db

    points = []
    hover_samples = []
    last_db = min_db
    for col in range(int(plot_w) + 1):
        db = buckets[col] if buckets[col] is not None else last_db
        last_db = db
        x = left + col
        freq_mhz = (low_hz + col / plot_w * (high_hz - low_hz)) / 1e6
        points.append((x, y_of(db)))
        hover_samples.append([x, round(freq_mhz, 3), round(db, 1)])

    stroke_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area_d = (
        f"M {points[0][0]:.1f},{baseline_y:.1f} L "
        + " L ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        + f" L {points[-1][0]:.1f},{baseline_y:.1f} Z"
    )

    freq_ticks = []
    tick_mhz = math.ceil(low_hz / 4_000_000) * 4
    while tick_mhz * 1_000_000 <= high_hz + 1:
        freq_ticks.append({"x": round(x_of(tick_mhz * 1_000_000), 1), "label": str(tick_mhz)})
        tick_mhz += 4

    db_ticks = [
        {"y": round(y_of(min_db), 1), "label": str(round(min_db))},
        {"y": round(y_of((min_db + max_db) / 2), 1), "label": str(round((min_db + max_db) / 2))},
        {"y": round(y_of(max_db), 1), "label": str(round(max_db))},
    ]

    peaks = [
        {
            "x": round(x_of(peak["freq_hz"]), 1),
            "y": round(y_of(peak["db"]), 1),
            "label_dy": -18 if i % 2 == 0 else -30,
            "freq_mhz": peak["freq_mhz"],
            "db": peak["db"],
            "name": peak.get("name"),
        }
        for i, peak in enumerate(scan_result["peaks"])
    ]

    return {
        "width": CHART_SIZE["width"],
        "height": CHART_SIZE["height"],
        "area_d": area_d,
        "stroke_d": stroke_d,
        "plot_top": top,
        "plot_bottom": round(baseline_y, 1),
        "plot_left": left,
        "plot_right": round(left + plot_w, 1),
        "freq_ticks": freq_ticks,
        "db_ticks": db_ticks,
        "peaks": peaks,
        "hover_samples": hover_samples,
    }


def start_adsb_tracking() -> subprocess.Popen:
    """Launch readsb against the dongle, writing a periodically-refreshed aircraft.json."""
    ADSB_JSON_DIR.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        (
            READSB_BIN, "--device-type", "rtlsdr", "--device", "0", "--gain", "-10",
            "--write-json", str(ADSB_JSON_DIR), "--write-json-every", "1", "--quiet",
        ),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def read_adsb_aircraft() -> list[dict[str, object]]:
    try:
        data = json.loads((ADSB_JSON_DIR / "aircraft.json").read_text())
    except (OSError, ValueError):
        return []
    aircraft = [
        {
            "hex": entry.get("hex", "").upper(),
            "flight": (entry.get("flight") or "").strip() or None,
            "lat": entry.get("lat"),
            "lon": entry.get("lon"),
            "alt_baro": entry.get("alt_baro"),
            "gs": entry.get("gs"),
            "track": entry.get("track"),
        }
        for entry in data.get("aircraft", [])
        if "lat" in entry and "lon" in entry
    ]
    aircraft.sort(key=lambda a: a["flight"] or a["hex"])
    return aircraft


def _bits_to_int(bits: str, signed: bool = False) -> int:
    value = int(bits, 2)
    if signed and bits[0] == "1":
        value -= 1 << len(bits)
    return value


def _armor_to_bits(payload: str) -> str:
    """Decode AIS's 6-bit ASCII armoring (AIVDM payload) into a raw bit string."""
    bits = []
    for char in payload:
        value = ord(char) - 48
        if value > 40:
            value -= 8
        bits.append(format(value, "06b"))
    return "".join(bits)


def _decode_ais_string(bits: str) -> str:
    chars = (AIS_CHARSET[_bits_to_int(bits[i : i + 6])] for i in range(0, len(bits) - 5, 6))
    return "".join(chars).rstrip("@ ").strip()


_ais_fragments: dict[str, list[str | None]] = {}


def _apply_ais_message(bits: str) -> None:
    if len(bits) < 38:
        return
    msg_type = _bits_to_int(bits[0:6])
    mmsi = _bits_to_int(bits[8:38])
    with AIS_LOCK:
        vessel = AIS_VESSELS.setdefault(
            mmsi, {"mmsi": mmsi, "name": None, "lat": None, "lon": None, "sog": None, "cog": None}
        )
        if msg_type in (1, 2, 3) and len(bits) >= 128:
            raw_lon = _bits_to_int(bits[61:89], signed=True)
            raw_lat = _bits_to_int(bits[89:116], signed=True)
            if raw_lon != 108_600_000 and raw_lat != 54_600_000:
                vessel["lon"] = round(raw_lon / 600_000.0, 5)
                vessel["lat"] = round(raw_lat / 600_000.0, 5)
            vessel["sog"] = round(_bits_to_int(bits[50:60]) / 10.0, 1)
            vessel["cog"] = round(_bits_to_int(bits[116:128]) / 10.0, 1)
        elif msg_type == 5 and len(bits) >= 232:
            name = _decode_ais_string(bits[112:232])
            if name:
                vessel["name"] = name
        vessel["last_seen"] = time.time()


def _decode_ais_line(line: str) -> None:
    """Parse one !AIVDM NMEA sentence, reassembling multi-part messages by sequence id."""
    body = line.strip().split("*", 1)[0]
    fields = body.split(",")
    if len(fields) < 7 or not (fields[0] == "!AIVDM" or fields[0] == "!AIVDO"):
        return
    try:
        total, frag_num = int(fields[1]), int(fields[2])
        payload, fill_bits = fields[5], int(fields[6])
    except ValueError:
        return

    if total == 1:
        bits = _armor_to_bits(payload)
        _apply_ais_message(bits[: len(bits) - fill_bits] if fill_bits else bits)
        return

    key = fields[3] or "_"
    parts = _ais_fragments.setdefault(key, [None] * total)
    if 0 <= frag_num - 1 < total:
        parts[frag_num - 1] = payload
    if all(part is not None for part in parts):
        bits = _armor_to_bits("".join(parts))
        _apply_ais_message(bits[: len(bits) - fill_bits] if fill_bits else bits)
        del _ais_fragments[key]


def start_ais_tracking() -> tuple[subprocess.Popen, threading.Thread]:
    """Launch rtl_ais against the dongle and pump its NMEA log into AIS_VESSELS."""
    process = subprocess.Popen(("rtl_ais", "-n"), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def pump() -> None:
        for line in process.stderr:
            _decode_ais_line(line)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return process, thread


def read_ais_vessels() -> list[dict[str, object]]:
    with AIS_LOCK:
        vessels = [dict(vessel) for vessel in AIS_VESSELS.values() if vessel.get("lat") is not None]
    vessels.sort(key=lambda vessel: vessel["name"] or str(vessel["mmsi"]))
    return vessels


def _process_lora_line(line: str) -> None:
    """Flag runs of several adjacent bins that rise together above their own historical
    baselines (not the pass's median, which is skewed by the dongle's per-hop filter shape).

    A single isolated bin spiking is far more likely to be broadband noise (this band is prone
    to RTL-SDR + USB3 self-interference on a Pi) than a real signal — a genuine LoRa chirp is
    125/250/500kHz wide and lights up several neighboring bins together, so only a qualifying
    run of LORA_MIN_RUN_BINS or more is treated as a candidate burst.

    Only the rising edge (a qualifying run newly appearing) is logged — one that keeps
    reappearing more often than a real LoRaWAN device would ever transmit gets squelched."""
    values = line.strip().split(",")
    if len(values) < 7:
        return
    try:
        low, step = float(values[2]), float(values[4])
        dbs = [float(v) for v in values[6:]]
    except ValueError:
        return

    now = time.time()
    keys = [round((low + i * step) / 1000) for i in range(len(dbs))]  # 1 kHz buckets: stable across passes

    elevated = []
    for key, db in zip(keys, dbs):
        baseline = LORA_BASELINE.get(key)
        if baseline is None:
            LORA_BASELINE[key] = db
            elevated.append(False)
        else:
            elevated.append(db - baseline >= LORA_BURST_THRESHOLD_DB)

    qualifying = [False] * len(dbs)
    i = 0
    while i < len(dbs):
        if not elevated[i]:
            i += 1
            continue
        j = i
        while j < len(dbs) and elevated[j]:
            j += 1
        if j - i >= LORA_MIN_RUN_BINS:
            qualifying[i:j] = [True] * (j - i)
        i = j

    i = 0
    while i < len(dbs):
        if not qualifying[i]:
            key = keys[i]
            LORA_ACTIVE.discard(key)
            baseline = LORA_BASELINE[key]
            LORA_BASELINE[key] = baseline * (1 - LORA_QUIET_ALPHA) + dbs[i] * LORA_QUIET_ALPHA
            i += 1
            continue

        j = i
        while j < len(dbs) and qualifying[j]:
            j += 1
        peak_offset = max(range(i, j), key=lambda idx: dbs[idx])
        peak_key = keys[peak_offset]
        for idx in range(i, j):
            LORA_BASELINE[keys[idx]] = LORA_BASELINE[keys[idx]] * (1 - LORA_BUSY_ALPHA) + dbs[idx] * LORA_BUSY_ALPHA

        if peak_key not in LORA_ACTIVE:
            LORA_ACTIVE.add(peak_key)
            freq_mhz = round((low + peak_offset * step) / 1e6, 3)
            if peak_key in LORA_SQUELCHED:
                pass
            else:
                triggers = LORA_TRIGGER_LOG.setdefault(peak_key, [])
                triggers.append(now)
                triggers[:] = [t for t in triggers if now - t < LORA_SQUELCH_WINDOW_SEC]
                if len(triggers) > LORA_SQUELCH_TRIGGER_COUNT:
                    LORA_SQUELCHED.add(peak_key)
                    with LORA_LOCK:
                        LORA_SIGHTINGS[:] = [s for s in LORA_SIGHTINGS if s["freq_mhz"] != freq_mhz]
                else:
                    with LORA_LOCK:
                        LORA_SIGHTINGS.insert(0, {
                            "ts": now,
                            "time": time.strftime("%H:%M:%S", time.localtime(now)),
                            "freq_mhz": freq_mhz,
                            "db_above_floor": round(dbs[peak_offset] - LORA_BASELINE[peak_key], 1),
                            "width_khz": round((j - i) * step / 1000, 1),
                        })
                        del LORA_SIGHTINGS[LORA_MAX_SIGHTINGS:]
        i = j


def start_lora_tracking() -> tuple[subprocess.Popen, threading.Thread]:
    """Continuously sweep the LoRa ISM sub-band, flagging energy bursts (not packet decoding)."""
    process = subprocess.Popen(
        ("rtl_power", "-f", LORA_RANGE, "-i", "1"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )

    def pump() -> None:
        for line in process.stdout:
            _process_lora_line(line)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return process, thread


def read_lora_sightings() -> list[dict[str, object]]:
    with LORA_LOCK:
        return list(LORA_SIGHTINGS)


def list_serial_ports() -> list[str]:
    ports: set[str] = set()
    for pattern in GNSS_PORT_GLOBS:
        ports.update(glob.glob(pattern))
    return sorted(ports)


def _nmea_checksum_ok(sentence: str) -> bool:
    body, star, checksum = sentence.partition("*")
    if not star or not body.startswith("$") or len(checksum) < 2:
        return False
    computed = 0
    for char in body[1:]:
        computed ^= ord(char)
    try:
        return computed == int(checksum[:2], 16)
    except ValueError:
        return False


def _nmea_coordinate(value: str, hemisphere: str, degree_digits: int) -> float | None:
    if not value:
        return None
    degrees = int(value[:degree_digits])
    minutes = float(value[degree_digits:])
    coordinate = degrees + minutes / 60
    return -coordinate if hemisphere in ("S", "W") else coordinate


def _log_gnss_event(message: str) -> None:
    """Append a milestone to GNSS_EVENTS. Caller must already hold GNSS_LOCK —
    this never acquires it itself, since Lock isn't reentrant and every call site
    is already inside a `with GNSS_LOCK:` block guarding a GNSS_FIX update."""
    now = time.time()
    GNSS_EVENTS.insert(0, {"ts": now, "time": time.strftime("%H:%M:%S", time.localtime(now)), "message": message})
    del GNSS_EVENTS[GNSS_EVENTS_MAX:]


def _check_fix_quality_transition(quality: str) -> None:
    """Log a GNSS_EVENTS entry only when GGA's fix-quality value actually changes
    (not on every GGA), the same rising-edge-only philosophy LoRa detection
    already uses to avoid a 1Hz flood of unchanged-state noise."""
    global GNSS_LAST_FIX_QUALITY
    if quality == GNSS_LAST_FIX_QUALITY:
        return
    new_label = GNSS_FIX_QUALITY.get(quality, f"Unknown ({quality})")
    if GNSS_LAST_FIX_QUALITY is None:
        _log_gnss_event(f"Fix acquired: {new_label}")
    else:
        prev_label = GNSS_FIX_QUALITY.get(GNSS_LAST_FIX_QUALITY, f"Unknown ({GNSS_LAST_FIX_QUALITY})")
        _log_gnss_event(f"Fix quality changed: {prev_label} → {new_label}")
    GNSS_LAST_FIX_QUALITY = quality


def _check_heading_transition(pos_type: str) -> None:
    """Same rising-edge logging as _check_fix_quality_transition, for the
    dual-antenna heading solution's own status (UNIHEADINGA pos_type)."""
    global GNSS_LAST_HEADING_POS_TYPE
    if pos_type == GNSS_LAST_HEADING_POS_TYPE:
        return
    new_label = GNSS_HEADING_POS_TYPE.get(pos_type, pos_type)
    if GNSS_LAST_HEADING_POS_TYPE is None:
        _log_gnss_event(f"Heading acquired: {new_label}")
    else:
        prev_label = GNSS_HEADING_POS_TYPE.get(GNSS_LAST_HEADING_POS_TYPE, GNSS_LAST_HEADING_POS_TYPE)
        _log_gnss_event(f"Heading quality changed: {prev_label} → {new_label}")
    GNSS_LAST_HEADING_POS_TYPE = pos_type


def _log_gnss_telemetry_if_due() -> None:
    """Append a full GNSS_FIX snapshot to GNSS_TELEMETRY, throttled to at most
    once per GNSS_TELEMETRY_MIN_INTERVAL — GGA/RMC/GSA (and UNIHEADINGA) usually
    arrive in a tight burst for the same epoch, so without this a single 1Hz
    update would otherwise log up to four near-duplicate rows. Caller must
    already hold GNSS_LOCK, same reentrancy note as _log_gnss_event."""
    global GNSS_LAST_TELEMETRY_TS
    now = time.time()
    if now - GNSS_LAST_TELEMETRY_TS < GNSS_TELEMETRY_MIN_INTERVAL:
        return
    GNSS_LAST_TELEMETRY_TS = now
    GNSS_TELEMETRY.insert(0, dict(GNSS_FIX))
    del GNSS_TELEMETRY[GNSS_TELEMETRY_MAX:]


def _apply_nmea_sentence(sentence: str) -> None:
    """Update GNSS_FIX from one NMEA-0183 sentence (GGA position/fix quality, RMC
    speed/course, GSA fix dimensionality) — talker id (GP/GN/GA/...) is ignored
    since the UM982 reports a combined GNSS solution under GN."""
    if not _nmea_checksum_ok(sentence):
        return
    fields = sentence.split("*", 1)[0].split(",")
    kind = fields[0][-3:]
    with GNSS_LOCK:
        if kind == "GGA" and len(fields) >= 10:
            GNSS_FIX["lat"] = _nmea_coordinate(fields[2], fields[3], 2)
            GNSS_FIX["lon"] = _nmea_coordinate(fields[4], fields[5], 3)
            GNSS_FIX["fix_quality"] = fields[6]
            GNSS_FIX["satellites"] = int(fields[7]) if fields[7] else None
            GNSS_FIX["hdop"] = float(fields[8]) if fields[8] else None
            GNSS_FIX["alt_m"] = float(fields[9]) if fields[9] else None
            _check_fix_quality_transition(fields[6])
        elif kind == "RMC" and len(fields) >= 9:
            GNSS_FIX["speed_knots"] = float(fields[7]) if fields[7] else None
            GNSS_FIX["course_deg"] = float(fields[8]) if fields[8] else None
        elif kind == "GSA" and len(fields) >= 3:
            GNSS_FIX["fix_type"] = fields[2] or None
        else:
            return
        now = time.time()
        GNSS_FIX["last_seen"] = now
        GNSS_FIX["updated"] = time.strftime("%H:%M:%S", time.localtime(now))
        _log_gnss_telemetry_if_due()


def _apply_unicore_log(line: str) -> None:
    """Update GNSS_FIX from a Unicore proprietary ASCII log — currently just
    UNIHEADINGA, the UM982's dual-antenna heading solution (azimuth from true
    north to the master->slave antenna baseline, plus its own quality/status
    fields), which has no standard-NMEA equivalent on this receiver. Field
    layout: Unicore Reference Commands Manual for N4 High Precision Products,
    section 7.3.48 / Table 7-118 (header;sol_stat,pos_type,length,heading,pitch,...)."""
    header, semi, body = line.split("*", 1)[0].partition(";")
    if not semi or not header.startswith("#UNIHEADINGA"):
        return
    fields = body.split(",")
    if len(fields) < 11:
        return
    with GNSS_LOCK:
        GNSS_FIX["heading_status"] = fields[0] or None
        GNSS_FIX["heading_pos_type"] = fields[1] or None
        GNSS_FIX["heading_baseline_m"] = float(fields[2]) if fields[2] else None
        GNSS_FIX["heading_deg"] = float(fields[3]) if fields[3] else None
        GNSS_FIX["pitch_deg"] = float(fields[4]) if fields[4] else None
        GNSS_FIX["heading_std_dev"] = float(fields[6]) if fields[6] else None
        GNSS_FIX["heading_satellites"] = int(fields[10]) if fields[10] else None
        _check_heading_transition(fields[1])
        now = time.time()
        GNSS_FIX["last_seen"] = now
        GNSS_FIX["updated"] = time.strftime("%H:%M:%S", time.localtime(now))
        _log_gnss_telemetry_if_due()


def _pump_gnss(connection: serial.Serial) -> None:
    while True:
        try:
            line = connection.readline().decode("ascii", errors="ignore").strip()
        except (OSError, serial.SerialException):
            return
        if not line:
            continue
        try:
            if line.startswith("#"):
                _apply_unicore_log(line)
            else:
                _apply_nmea_sentence(line)
        except (ValueError, IndexError):
            continue


def start_gnss_tracking(port: str, baud: int) -> tuple[serial.Serial, threading.Thread]:
    connection = serial.Serial(port, baudrate=baud, timeout=2)
    thread = threading.Thread(target=_pump_gnss, args=(connection,), daemon=True)
    thread.start()
    return connection, thread


def read_gnss_fix() -> dict[str, object]:
    with GNSS_LOCK:
        return dict(GNSS_FIX)


def read_gnss_events() -> list[dict[str, object]]:
    with GNSS_LOCK:
        return list(GNSS_EVENTS)


def read_gnss_telemetry() -> list[dict[str, object]]:
    with GNSS_LOCK:
        return list(GNSS_TELEMETRY)


def _looks_like_gnss_data(text: str) -> bool:
    for line in text.splitlines():
        line = line.strip()
        if _nmea_checksum_ok(line):
            return True
        if line.startswith("#") and ";" in line and "*" in line:
            return True
    return False


def detect_gnss_receiver() -> tuple[str, int] | None:
    """Try every /dev/ttyUSB*|ttyACM* port at each of GNSS_PROBE_BAUDS, reading
    for a bounded window at each combination, until one produces a recognizable
    NMEA sentence or Unicore ASCII log — the wrong baud rate on the right port
    just reads back framing-error noise, which won't checksum or look like a log,
    so it's naturally rejected without needing to know the port in advance."""
    for port in list_serial_ports():
        for baud in GNSS_PROBE_BAUDS:
            try:
                with serial.Serial(port, baudrate=baud, timeout=0.6) as connection:
                    connection.reset_input_buffer()
                    raw = connection.read(2000)
            except (OSError, serial.SerialException):
                continue
            if _looks_like_gnss_data(raw.decode("ascii", errors="ignore")):
                return port, baud
    return None


@app.get("/")
@require_auth
def index():
    radios = {radio["device"]: radio for radio in status()}
    fm_chart = build_fm_chart(LAST_FM_SCAN) if LAST_FM_SCAN and "error" not in LAST_FM_SCAN else None
    return render_template(
        "index.html",
        radios=radios,
        management_mode=wireless_mode(MANAGEMENT, radios.get(MANAGEMENT)),
        field_mode=wireless_mode(FIELD, radios.get(FIELD)),
        management_networks=scan(MANAGEMENT),
        management_profiles=wireless_profiles(MANAGEMENT),
        field_networks=scan(FIELD),
        fm_chart=fm_chart,
        fm_peaks=(LAST_FM_SCAN or {}).get("peaks", []),
        active_sdr_mode=ACTIVE_SDR_SESSION["mode"] if ACTIVE_SDR_SESSION else None,
        adsb_aircraft=read_adsb_aircraft() if ACTIVE_SDR_SESSION and ACTIVE_SDR_SESSION["mode"] == "adsb" else [],
        ais_vessels=read_ais_vessels() if ACTIVE_SDR_SESSION and ACTIVE_SDR_SESSION["mode"] == "ais" else [],
        lora_sightings=read_lora_sightings() if ACTIVE_SDR_SESSION and ACTIVE_SDR_SESSION["mode"] == "lora" else [],
        lora_squelched_count=len(LORA_SQUELCHED),
        gnss_active=GNSS_SESSION is not None,
        gnss_ports=list_serial_ports(),
        gnss_default_baud=GNSS_DEFAULT_BAUD,
        gnss_fix=read_gnss_fix() if GNSS_SESSION is not None else None,
        gnss_fix_quality=GNSS_FIX_QUALITY,
        gnss_heading_pos_type=GNSS_HEADING_POS_TYPE,
        gnss_heading_sol_status=GNSS_HEADING_SOL_STATUS,
        gnss_detected=GNSS_DETECTED,
        gnss_events=read_gnss_events(),
        gnss_telemetry=read_gnss_telemetry(),
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


def dongle_busy_redirect(anchor: str):
    """Refuse to start a new SDR session while another one holds the (single) dongle."""
    if ACTIVE_SDR_SESSION is not None:
        flash(f"Stop {ACTIVE_SDR_SESSION['mode'].upper()} tracking first — the dongle is in use.", "error")
        return redirect(url_for("index", _anchor=anchor))
    return None


@app.post("/sdr/fm-scan")
@require_auth
def sdr_fm_scan():
    global LAST_FM_SCAN
    busy = dongle_busy_redirect("fm-scan-tab")
    if busy:
        return busy
    LAST_FM_SCAN = run_fm_scan()
    if "error" in LAST_FM_SCAN:
        flash(LAST_FM_SCAN["error"], "error")
    else:
        flash(f"FM scan complete: {len(LAST_FM_SCAN['peaks'])} station(s) found.", "ok")
    return redirect(url_for("index", _anchor="fm-scan-tab"))


@app.post("/sdr/fm-identify")
@require_auth
def sdr_fm_identify():
    busy = dongle_busy_redirect("fm-scan-tab")
    if busy:
        return busy
    try:
        freq_mhz = float(request.form.get("freq_mhz", ""))
    except ValueError:
        flash("Invalid frequency.", "error")
        return redirect(url_for("index", _anchor="fm-scan-tab"))
    if not 87.0 <= freq_mhz <= 109.0:
        flash("Frequency must be within the FM broadcast band.", "error")
        return redirect(url_for("index", _anchor="fm-scan-tab"))

    result = identify_fm_station(freq_mhz * 1e6)
    if LAST_FM_SCAN and "peaks" in LAST_FM_SCAN:
        for peak in LAST_FM_SCAN["peaks"]:
            if abs(peak["freq_mhz"] - freq_mhz) < 0.05:
                peak["name"] = result.get("name")
    if "error" in result:
        flash(result["error"], "error")
    else:
        flash(f"{freq_mhz} MHz: {result['name']}", "ok")
    return redirect(url_for("index", _anchor="fm-scan-tab"))


@app.post("/sdr/adsb/start")
@require_auth
def sdr_adsb_start():
    global ACTIVE_SDR_SESSION
    busy = dongle_busy_redirect("adsb-tab")
    if busy:
        return busy
    ACTIVE_SDR_SESSION = {"mode": "adsb", "process": start_adsb_tracking()}
    flash("ADS-B tracking started.", "ok")
    return redirect(url_for("index", _anchor="adsb-tab"))


@app.post("/sdr/adsb/stop")
@require_auth
def sdr_adsb_stop():
    global ACTIVE_SDR_SESSION
    if ACTIVE_SDR_SESSION and ACTIVE_SDR_SESSION["mode"] == "adsb":
        ACTIVE_SDR_SESSION["process"].terminate()
        ACTIVE_SDR_SESSION["process"].wait(timeout=5)
        ACTIVE_SDR_SESSION = None
        flash("ADS-B tracking stopped.", "ok")
    return redirect(url_for("index", _anchor="adsb-tab"))


@app.post("/sdr/ais/start")
@require_auth
def sdr_ais_start():
    global ACTIVE_SDR_SESSION
    busy = dongle_busy_redirect("ais-tab")
    if busy:
        return busy
    process, thread = start_ais_tracking()
    ACTIVE_SDR_SESSION = {"mode": "ais", "process": process, "thread": thread}
    with AIS_LOCK:
        AIS_VESSELS.clear()
    flash("AIS tracking started.", "ok")
    return redirect(url_for("index", _anchor="ais-tab"))


@app.post("/sdr/ais/stop")
@require_auth
def sdr_ais_stop():
    global ACTIVE_SDR_SESSION
    if ACTIVE_SDR_SESSION and ACTIVE_SDR_SESSION["mode"] == "ais":
        ACTIVE_SDR_SESSION["process"].terminate()
        ACTIVE_SDR_SESSION["process"].wait(timeout=5)
        ACTIVE_SDR_SESSION = None
        flash("AIS tracking stopped.", "ok")
    return redirect(url_for("index", _anchor="ais-tab"))


@app.post("/sdr/lora/start")
@require_auth
def sdr_lora_start():
    global ACTIVE_SDR_SESSION
    busy = dongle_busy_redirect("lora-tab")
    if busy:
        return busy
    process, thread = start_lora_tracking()
    ACTIVE_SDR_SESSION = {"mode": "lora", "process": process, "thread": thread}
    with LORA_LOCK:
        LORA_SIGHTINGS.clear()
    LORA_BASELINE.clear()
    LORA_ACTIVE.clear()
    LORA_TRIGGER_LOG.clear()
    LORA_SQUELCHED.clear()
    flash("LoRa activity detector started.", "ok")
    return redirect(url_for("index", _anchor="lora-tab"))


@app.post("/sdr/lora/stop")
@require_auth
def sdr_lora_stop():
    global ACTIVE_SDR_SESSION
    if ACTIVE_SDR_SESSION and ACTIVE_SDR_SESSION["mode"] == "lora":
        ACTIVE_SDR_SESSION["process"].terminate()
        ACTIVE_SDR_SESSION["process"].wait(timeout=5)
        ACTIVE_SDR_SESSION = None
        flash("LoRa activity detector stopped.", "ok")
    return redirect(url_for("index", _anchor="lora-tab"))


@app.post("/gnss/detect")
@require_auth
def gnss_detect():
    global GNSS_DETECTED
    if GNSS_SESSION is not None:
        flash("Stop the current GNSS session before detecting.", "error")
        return redirect(url_for("index", _anchor="gnss-tab"))
    if not list_serial_ports():
        flash("No /dev/ttyUSB* or /dev/ttyACM* device detected — plug in the receiver first.", "error")
        return redirect(url_for("index", _anchor="gnss-tab"))
    found = detect_gnss_receiver()
    if found is None:
        GNSS_DETECTED = None
        flash("No GNSS data found on any detected port/baud combination.", "error")
        return redirect(url_for("index", _anchor="gnss-tab"))
    port, baud = found
    GNSS_DETECTED = {"port": port, "baud": baud}
    flash(f"Detected GNSS receiver on {port} @ {baud} baud.", "ok")
    return redirect(url_for("index", _anchor="gnss-tab"))


@app.post("/gnss/start")
@require_auth
def gnss_start():
    global GNSS_SESSION, GNSS_LAST_FIX_QUALITY, GNSS_LAST_HEADING_POS_TYPE, GNSS_LAST_TELEMETRY_TS
    if GNSS_SESSION is not None:
        flash("GNSS tracking is already running.", "error")
        return redirect(url_for("index", _anchor="gnss-tab"))
    port = request.form.get("port", "").strip()
    if not valid_name(port):
        flash("Enter a valid serial port.", "error")
        return redirect(url_for("index", _anchor="gnss-tab"))
    try:
        baud = int(request.form.get("baud", GNSS_DEFAULT_BAUD))
    except ValueError:
        flash("Invalid baud rate.", "error")
        return redirect(url_for("index", _anchor="gnss-tab"))
    try:
        connection, thread = start_gnss_tracking(port, baud)
    except serial.SerialException as error:
        flash(f"Could not open {port}: {error}", "error")
        return redirect(url_for("index", _anchor="gnss-tab"))
    GNSS_SESSION = {"serial": connection, "thread": thread}
    with GNSS_LOCK:
        GNSS_FIX.clear()
        # New session = fresh transition baseline, even though GNSS_EVENTS/GNSS_TELEMETRY
        # themselves are kept across start/stop cycles — so the first fix/heading of this
        # session always logs as "acquired", not silently compared to a stale prior value.
        GNSS_LAST_FIX_QUALITY = None
        GNSS_LAST_HEADING_POS_TYPE = None
        GNSS_LAST_TELEMETRY_TS = 0.0
        _log_gnss_event(f"GNSS tracking started on {port} @ {baud} baud.")
    flash(f"GNSS tracking started on {port} @ {baud} baud.", "ok")
    return redirect(url_for("index", _anchor="gnss-tab"))


@app.post("/gnss/stop")
@require_auth
def gnss_stop():
    global GNSS_SESSION
    if GNSS_SESSION is not None:
        GNSS_SESSION["serial"].close()
        GNSS_SESSION["thread"].join(timeout=5)
        GNSS_SESSION = None
        with GNSS_LOCK:
            _log_gnss_event("GNSS tracking stopped.")
        flash("GNSS tracking stopped.", "ok")
    return redirect(url_for("index", _anchor="gnss-tab"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)

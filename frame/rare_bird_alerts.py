#!/usr/bin/env python3
"""Phone alerts when a rare bird is heard, for the AvianVisitors frame.

Runs on the frame Pi every 10 minutes (install_rare_alerts.sh sets that up).
Each run asks BirdWeather which birds were heard recently and sends a push
notification through ntfy (https://ntfy.sh) for any that are:
  - rare here: heard only a few times (or never) in the previous 30 days, or
  - on your watchlist: always worth an alert, however common.

To cut down on BirdNET's occasional misidentifications, a bird must be heard
at least twice, at 75%+ confidence, before it triggers an alert, and each bird
alerts at most once a day. All of this is adjustable in ~/.birdframe/alerts.toml.

Both phones install the free ntfy app and subscribe to the same topic name
(printed by --init and stored in alerts.toml).

Usage (from AvianVisitors/frame, with the frame's Python):
  .venv/bin/python rare_bird_alerts.py --init     # write alerts.toml (once)
  .venv/bin/python rare_bird_alerts.py --test     # send a test notification
  .venv/bin/python rare_bird_alerts.py --dry-run  # show what would be sent
  .venv/bin/python rare_bird_alerts.py            # check now and send alerts
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

FRAME_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(FRAME_DIR))
import birdweather  # noqa: E402  (the project's own BirdWeather client)

STATE_DIR = Path.home() / ".birdframe"
ALERTS = STATE_DIR / "alerts.toml"
STATE = STATE_DIR / "alerts-state.json"
FRAME_CONFIG = STATE_DIR / "config.toml"

DEFAULTS = {
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "follow_frame": False,   # true = use whatever location the frame shows
    "station": "",           # a BirdWeather station number
    "postcode": "",          # or a postcode (stations within radius_km)
    "country": "au",         # for a postcode
    "radius_km": 10,         # for a postcode: stations within this distance
    "place_name": "",        # used in the message, e.g. "Walter St"
    "rare_max": 3,           # rare = heard at most this many times in history_days
    "history_days": 30,
    "window_hours": 1,       # how far back each check looks
    "min_confidence": 0.75,  # BirdNET confidence needed (0 to 1)
    "min_detections": 2,     # confident detections needed in the window
    "cooldown_hours": 24,    # don't repeat the same bird sooner than this
    "watchlist": [],         # always alert for these (common or scientific names)
    "ignore": [],            # never alert for these
}

ALERTS_TEMPLATE = """\
# Rare bird alerts for the AvianVisitors frame (rare_bird_alerts.py).
# Changes take effect on the next check (every 10 minutes); nothing to restart.

# Both phones: install the ntfy app and subscribe to this topic on ntfy.sh.
# Treat the name like a password: anyone who knows it can read the alerts.
ntfy_server = "https://ntfy.sh"
ntfy_topic = {topic}

# Where to listen for birds. Set station OR postcode.
#   follow_frame = true  uses whatever location the frame is showing instead,
#   so alerts change when you switch locations on the phone page.
follow_frame = {follow}
station = {station}
postcode = {postcode}
country = {country}
radius_km = 10
place_name = {place}

# A bird is "rare here" if it was heard at most rare_max times in the
# previous history_days days (0 = a first in that time).
rare_max = 3
history_days = 30

# Guard against BirdNET misidentifications: a bird must be heard at least
# min_detections times in the last window_hours, at min_confidence or higher.
window_hours = 1
min_confidence = 0.75
min_detections = 2

# Each bird alerts at most once per cooldown_hours.
cooldown_hours = 24

# Always alert for these, however common; never alert for those in ignore.
# Use common or scientific names, e.g. ["Powerful Owl", "Gang-gang Cockatoo"]
watchlist = []
ignore = []
"""

QUERY = """
query RareBirdAlerts($ids: [ID!]!, $recent: InputDuration!, $history: InputDuration!, $conf: Float!) {
  recent: detections(stationIds: $ids, period: $recent, confidenceGte: $conf, first: 500) {
    nodes { timestamp confidence species { commonName scientificName } }
  }
  recentAll: topSpecies(stationIds: $ids, period: $recent, limit: 200) {
    count species { scientificName }
  }
  history: topSpecies(stationIds: $ids, period: $history, limit: 500) {
    count species { scientificName }
  }
}
""".strip()


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def load_toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def load_settings():
    cfg = dict(DEFAULTS)
    cfg.update(load_toml(ALERTS))
    for key in ("watchlist", "ignore"):
        if not isinstance(cfg[key], list):
            cfg[key] = [cfg[key]]
        cfg[key] = [str(x).strip().lower() for x in cfg[key] if str(x).strip()]
    return cfg


# --- where to listen ----------------------------------------------------------
def resolve_watch(cfg):
    """Return (station ids, place name, link) for the configured location."""
    station = str(cfg.get("station") or "").strip()
    postcode = str(cfg.get("postcode") or "").strip()
    country = str(cfg.get("country") or "au").strip().lower()
    place = str(cfg.get("place_name") or "").strip()
    if cfg.get("follow_frame"):
        frame = load_toml(FRAME_CONFIG)
        place = str(frame.get("shoot_location") or "").strip()  # the frame's own name for it
        station, postcode = "", ""
        if frame.get("species_source") == "birdweather" and frame.get("bw_station_id"):
            station = str(frame["bw_station_id"])
        elif frame.get("species_source") == "birdweather" and frame.get("zip"):
            postcode, country = str(frame["zip"]).strip(), str(frame.get("bw_country", "au")).lower()
        else:
            raise SystemExit("The frame is showing a bird mic, not a BirdWeather location. "
                             "BirdNET-Pi has its own notifications (Tools > Settings > "
                             "Notifications); or set a station in alerts.toml.")
    if station:
        try:
            sid = birdweather.station_id(station)
        except ValueError:
            raise SystemExit(f"station = {station!r} in alerts.toml isn't a BirdWeather station number.")
        return [sid], place or f"station {sid}", f"https://app.birdweather.com/stations/{sid}"
    if not postcode:
        raise SystemExit("Set station or postcode in alerts.toml.")
    lat, lon = birdweather.geocode(postcode, country=country)
    max_miles = float(cfg["radius_km"]) / 1.609
    near = [sid for miles, sid in birdweather.nearest_stations(lat, lon, n=10) if miles <= max_miles]
    if not near:
        raise SystemExit(f"No BirdWeather stations within {cfg['radius_km']} km of postcode {postcode}.")
    return [str(s) for s in near], place or f"near postcode {postcode}", "https://app.birdweather.com/"


# --- finding rare birds ---------------------------------------------------------
def fetch(cfg, ids):
    payload = birdweather._graphql(QUERY, 30, variables={
        "ids": ids,
        "recent": {"count": int(cfg["window_hours"]), "unit": "hour"},
        "history": {"count": int(cfg["history_days"]), "unit": "day"},
        "conf": float(cfg["min_confidence"]),
    }, strict=True)
    return payload["data"]


def counts(rows):
    out = {}
    for row in rows or []:
        sci = ((row or {}).get("species") or {}).get("scientificName")
        if isinstance(sci, str) and isinstance(row.get("count"), int):
            out[sci] = out.get(sci, 0) + row["count"]
    return out


def find_alerts(cfg, data):
    """Birds worth an alert, each with the reason, from one BirdWeather reply."""
    heard = {}
    for d in ((data.get("recent") or {}).get("nodes") or []):
        sp = (d or {}).get("species") or {}
        sci, com = sp.get("scientificName"), sp.get("commonName") or sp.get("scientificName")
        if not isinstance(sci, str):
            continue
        h = heard.setdefault(sci, {"sci": sci, "com": com, "n": 0, "best": 0.0, "first": None})
        h["n"] += 1
        h["best"] = max(h["best"], float(d.get("confidence") or 0))
        ts = d.get("timestamp")
        if isinstance(ts, str) and (h["first"] is None or ts < h["first"]):
            h["first"] = ts
    recent_all, history = counts(data.get("recentAll")), counts(data.get("history"))
    # a brand-new station has no history yet, so everything would look rare
    established = len(history) >= 10 and sum(history.values()) >= 200

    alerts = []
    for h in heard.values():
        names = {h["sci"].lower(), str(h["com"]).lower()}
        if names & set(cfg["ignore"]) or h["n"] < int(cfg["min_detections"]):
            continue
        prior = max(0, history.get(h["sci"], 0) - recent_all.get(h["sci"], 0))
        if names & set(cfg["watchlist"]):
            h["reason"], h["watch"] = "On your watchlist.", True
        elif established and prior <= int(cfg["rare_max"]):
            days = int(cfg["history_days"])
            h["reason"] = (f"First time heard here in {days} days." if prior == 0 else
                           f"Only heard {prior} time{'s' if prior != 1 else ''} here in the last {days} days.")
            h["watch"] = False
        else:
            continue
        alerts.append(h)
    return alerts, established


# --- sending ----------------------------------------------------------------------
def nice_time(ts):
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return t.strftime("%I:%M%p").lstrip("0").lower()
    except (AttributeError, ValueError):
        return None


def send(cfg, title, message, click=None, priority=3, tags=("bird",)):
    topic = str(cfg["ntfy_topic"]).strip()
    if not topic:
        raise SystemExit("No ntfy_topic in alerts.toml. Run with --init first.")
    body = {"topic": topic, "title": title, "message": message,
            "priority": priority, "tags": list(tags)}
    if click:
        body["click"] = click
    req = urllib.request.Request(
        str(cfg["ntfy_server"]).rstrip("/") + "/", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "AvianVisitors-frame/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read(10_000)


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE)


def check(cfg, dry_run=False):
    ids, place, link = resolve_watch(cfg)
    data = fetch(cfg, ids)
    alerts, established = find_alerts(cfg, data)
    if not established:
        log(f"{place}: not enough history yet for rarity, so only watchlist birds alert")
    state, now = load_state(), time.time()
    cooldown = float(cfg["cooldown_hours"]) * 3600
    # forget old entries so the state file stays small
    state = {k: v for k, v in state.items() if now - float(v) < max(cooldown, 86400) * 2}
    sent = 0
    for a in alerts:
        key = f"{','.join(ids)}|{a['sci']}"
        if now - float(state.get(key, 0)) < cooldown:
            continue
        when = nice_time(a["first"])
        msg = (f"{a['com']} heard {a['n']} time{'s' if a['n'] != 1 else ''}{'' if place.startswith('near ') else ' at'} {place}"
               f"{f' since {when}' if when else ''} (best match {round(a['best'] * 100)}%). "
               f"{a['reason']}")
        title = f"Rare bird: {a['com']}" if not a["watch"] else f"Watchlist bird: {a['com']}"
        if dry_run:
            log(f"would send: {title} | {msg}")
        else:
            send(cfg, title, msg, click=link, priority=4)
            state[key] = now
            log(f"sent: {title}")
        sent += 1
    if not dry_run:
        save_state(state)
    if not sent:
        log(f"{place}: nothing rare in the last {cfg['window_hours']} hour(s)")


def init():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if ALERTS.exists():
        cfg = load_settings()
        print(f"{ALERTS} already exists; keeping it.")
    else:
        frame = load_toml(FRAME_CONFIG)
        station = str(frame.get("bw_station_id") or "")
        postcode = "" if station else str(frame.get("zip") or "")
        follow = "false" if (station or postcode) else "true"
        country = str(frame.get("bw_country") or "au").lower()
        place = str(frame.get("shoot_location") or "")
        topic = "birdframe-" + secrets.token_hex(6)
        ALERTS.write_text(ALERTS_TEMPLATE.format(
            topic=json.dumps(topic), follow=follow, station=json.dumps(station),
            postcode=json.dumps(postcode), country=json.dumps(country), place=json.dumps(place)),
            encoding="utf-8")
        cfg = load_settings()
        print(f"Wrote {ALERTS}")
    print("\nOn both phones:")
    print("  1. Install the free 'ntfy' app (App Store or Google Play).")
    print("  2. Tap + (Subscribe to topic) and enter this topic name:")
    print(f"       {cfg['ntfy_topic']}")
    print(f"     (server: {cfg['ntfy_server']}, the app's default)")


def main():
    ap = argparse.ArgumentParser(description="Phone alerts for rare birds.")
    ap.add_argument("--init", action="store_true", help="create alerts.toml and show the topic")
    ap.add_argument("--test", action="store_true", help="send a test notification")
    ap.add_argument("--dry-run", action="store_true", help="show alerts without sending them")
    args = ap.parse_args()
    if args.init:
        return init()
    cfg = load_settings()
    if args.test:
        send(cfg, "Bird frame alerts are on",
             "This is a test. You'll get a message like this when a rare bird is heard.", priority=3)
        return log(f"test notification sent to topic {cfg['ntfy_topic']}")
    try:
        check(cfg, dry_run=args.dry_run)
    except birdweather.BirdWeatherError as e:
        log(f"BirdWeather lookup failed ({e}); will try again next time")
    except OSError as e:
        log(f"network problem ({e}); will try again next time")


if __name__ == "__main__":
    main()

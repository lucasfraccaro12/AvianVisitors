#!/usr/bin/env python3
"""Phone-friendly page for choosing where the AvianVisitors frame gets its birds.

Runs on the frame Pi and serves a small web page on your home network,
e.g. http://birdpic.local:8080. Tap a saved location and the page rewrites the
source lines in ~/.birdframe/config.toml, then asks the frame to redraw
straight away. Everything else in the config (title, rotation, opening, quiet
hours...) is left as it is.

A location can be:
  - a public BirdWeather station (its number, e.g. 30108)
  - a postcode (birds from BirdWeather stations nearby)
  - a bird mic / AvianVisitors site (e.g. http://birdnet.local)

Saved locations live in ~/.birdframe/locations.json. Before each change the
previous config is copied to ~/.birdframe/config.toml.bak.

Put this file in AvianVisitors/frame/ and run it with the frame's own Python:
  .venv/bin/python location_switcher.py
(install_location_switcher.sh sets it up to start on boot.)

The page has no password, so anyone on your Wi-Fi can change the location.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

FRAME_DIR = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".birdframe"
sys.path.insert(0, str(FRAME_DIR))

# Keys that choose the bird source. The switcher owns these and nothing else.
SOURCE_KEYS = {"species_source", "zip", "bw_station_id", "bw_country",
               "base_url", "image_url", "image", "shoot", "shoot_location"}
BLOCK_START = "# >>> location (set by the location switcher; edit on the web page)"
BLOCK_END = "# <<< location"

KINDS = {
    "station": "BirdWeather station",
    "postcode": "Postcode",
    "mic": "Bird mic / website",
}

DEFAULT_LOCATIONS = [
    {"id": "walter-st", "name": "Walter St", "kind": "station", "value": "30108", "country": "au"},
]


# --- validation ---------------------------------------------------------------
def clean_location(form):
    """Check a submitted location. Returns (location, error message)."""
    name = " ".join((form.get("name") or "").split())[:60]
    kind = form.get("kind") or ""
    value = (form.get("value") or "").strip()
    country = (form.get("country") or "au").strip().lower()
    if kind not in KINDS:
        return None, "Choose what kind of location this is."
    if kind == "station":
        if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > 2147483647:
            return None, ("A BirdWeather station is the number at the end of its page address, "
                          "e.g. 30108.")
    elif kind == "postcode":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{0,8}[A-Za-z0-9]", value):
            return None, "That doesn't look like a postcode."
        if not re.fullmatch(r"[a-z]{2}", country):
            return None, "Country should be a two-letter code, e.g. au."
    else:
        if not re.fullmatch(r"https?://[A-Za-z0-9._~:/?#@!$&()*+,;=%-]+", value):
            return None, "The address should start with http:// or https://, e.g. http://birdnet.local"
    if not name:
        name = {"station": f"Station {value}", "postcode": f"Postcode {value}"}.get(kind, value)
    return {"id": uuid.uuid4().hex[:8], "name": name, "kind": kind, "value": value,
            "country": country}, None


# --- saved locations ------------------------------------------------------------
class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict) and d.get("kind") in KINDS]
        except (OSError, ValueError):
            pass
        return [dict(d) for d in DEFAULT_LOCATIONS]

    def save(self, items):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


# --- config file ----------------------------------------------------------------
def read_config(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def current_source(cfg):
    """(kind, value, country) the frame is using now, as best we can tell."""
    if cfg.get("species_source") == "birdweather":
        if cfg.get("bw_station_id") not in (None, "", 0):
            return ("station", str(cfg["bw_station_id"]), None)
        return ("postcode", str(cfg.get("zip", "")).strip(), str(cfg.get("bw_country", "us")).lower())
    if cfg.get("image_url") and not cfg.get("shoot"):
        return ("image", cfg["image_url"], None)
    return ("mic", cfg.get("base_url", "http://birdnet.local"), None)


def is_current(loc, cur):
    kind, value, country = cur
    if loc["kind"] != kind or loc["value"].lower() != (value or "").lower():
        return False
    return kind != "postcode" or loc.get("country", "au") == country


def source_lines(loc):
    # shown on the frame as "in <name>" under the headline (needs the frame patch)
    label = f"shoot_location = {json.dumps(loc['name'])}"
    if loc["kind"] == "station":
        lines = ['species_source = "birdweather"', f'bw_station_id = "{loc["value"]}"', "shoot = true"]
    elif loc["kind"] == "postcode":
        lines = ['species_source = "birdweather"', f'zip = "{loc["value"]}"',
                 f'bw_country = "{loc["country"]}"', "shoot = true"]
    else:
        lines = [f'base_url = "{loc["value"]}"', "shoot = true"]
    return lines + [label]


def write_config(path, loc):
    """Replace only the source lines of the config, keeping everything else."""
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    kept, inside = [], False
    for line in original.splitlines():
        stripped = line.strip()
        if stripped == BLOCK_START:
            inside = True
            continue
        if inside:
            if stripped == BLOCK_END:
                inside = False
            continue
        if stripped.startswith("# birdframe-mode:"):
            mode = "birdweather" if loc["kind"] in ("station", "postcode") else "local"
            kept.append(f"# birdframe-mode: {mode}")
            continue
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in SOURCE_KEYS:
            continue
        kept.append(line)
    block = [BLOCK_START, *source_lines(loc), BLOCK_END]
    # top-level keys must come before any [table]; the frame config has none,
    # but be safe if one is ever added
    first_table = next((i for i, l in enumerate(kept) if l.strip().startswith("[")), len(kept))
    while first_table > 0 and not kept[first_table - 1].strip():
        first_table -= 1
    new_lines = kept[:first_table] + [""] + block + kept[first_table:]
    text = "\n".join(new_lines).strip("\n") + "\n"
    tomllib.loads(text)  # refuse to write a config the frame couldn't read
    if original:
        (path.parent / (path.name + ".bak")).write_text(original, encoding="utf-8")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --- redraw ---------------------------------------------------------------------
class Refresher:
    def __init__(self, config, log_path):
        self.config, self.log_path = config, log_path
        self.proc, self.started, self.lock = None, 0.0, threading.Lock()

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return  # one already running; it will read the new config
            log = open(self.log_path, "w", encoding="utf-8")
            # --wait-lock queues behind a timer run instead of skipping
            self.proc = subprocess.Popen(
                [sys.executable, str(FRAME_DIR / "display.py"), "--config", str(self.config),
                 "--force", "--wait-lock"],
                cwd=str(FRAME_DIR), stdout=log, stderr=subprocess.STDOUT)
            self.started = time.time()

    def status(self):
        running = bool(self.proc and self.proc.poll() is None)
        try:
            lines = [l for l in self.log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        except OSError:
            lines = []
        last = lines[-1] if lines else ""
        ok = None if running or not lines else last.startswith(("panel updated", "wrote preview"))
        return {"running": running, "last": last, "ok": ok,
                "seconds": int(time.time() - self.started) if self.started else 0}


# --- page -----------------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Bird Frame">
<meta name="theme-color" content="#3b5b4a">
<title>Bird Frame</title>
<style>
:root {{ --bg:#f3f1ea; --card:#fff; --ink:#1f1f1c; --muted:#6b6a63; --line:#dedbd0;
        --accent:#3b5b4a; --accent-soft:#e3ece6; --bad:#a3392f; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1c1c1a; --card:#272724; --ink:#f0eee6;
        --muted:#a9a79e; --line:#3a3a35; --accent:#8fc1a5; --accent-soft:#2b3a31; --bad:#e08a80; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
        font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:520px; margin:0 auto; padding:20px 16px 48px; }}
h1 {{ font-size:22px; margin:6px 0 2px; }}
.sub {{ color:var(--muted); margin:0 0 18px; font-size:14px; }}
.status {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px;
          margin-bottom:18px; font-size:14px; display:flex; gap:10px; align-items:center; }}
.dot {{ width:10px; height:10px; border-radius:50%; background:var(--muted); flex:none; }}
.status.busy .dot {{ background:#d9a400; animation:pulse 1.2s infinite; }}
.status.ok .dot {{ background:#3a9d5d; }}
.status.bad .dot {{ background:var(--bad); }}
@keyframes pulse {{ 50% {{ opacity:.3; }} }}
.list {{ display:flex; flex-direction:column; gap:10px; }}
.loc {{ display:flex; align-items:stretch; background:var(--card); border:1px solid var(--line);
       border-radius:14px; overflow:hidden; }}
.loc.on {{ border-color:var(--accent); box-shadow:0 0 0 2px var(--accent) inset; }}
.loc form.pick {{ flex:1; margin:0; }}
.pick button {{ width:100%; text-align:left; background:none; border:0; padding:14px 16px; font:inherit;
               color:inherit; cursor:pointer; min-height:64px; }}
.pick b {{ display:block; font-size:17px; }}
.pick span {{ color:var(--muted); font-size:13px; }}
.badge {{ display:inline-block; margin-left:6px; padding:1px 8px; border-radius:99px; font-size:12px;
         background:var(--accent-soft); color:var(--accent); font-weight:600; vertical-align:2px; }}
.loc form.del {{ margin:0; display:flex; }}
.del button {{ background:none; border:0; border-left:1px solid var(--line); color:var(--muted);
              padding:0 16px; font-size:20px; cursor:pointer; }}
h2 {{ font-size:16px; margin:28px 0 10px; }}
.add {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px;
       display:grid; gap:10px; }}
label {{ font-size:13px; color:var(--muted); display:grid; gap:4px; }}
input, select {{ font:inherit; padding:10px 12px; border-radius:10px; border:1px solid var(--line);
                background:var(--bg); color:var(--ink); width:100%; }}
.row {{ display:grid; grid-template-columns:1fr 90px; gap:10px; }}
.primary {{ font:inherit; font-weight:600; background:var(--accent); color:#fff; border:0; border-radius:10px;
           padding:12px; cursor:pointer; }}
@media (prefers-color-scheme: dark) {{ .primary {{ color:#10241a; }} }}
.secondary {{ font:inherit; background:none; border:1px solid var(--line); color:var(--ink); border-radius:10px;
             padding:10px 12px; cursor:pointer; width:100%; margin-top:10px; }}
.msg {{ padding:10px 12px; border-radius:10px; margin-bottom:14px; font-size:14px; }}
.msg.err {{ background:#a3392f22; color:var(--bad); }}
.msg.ok {{ background:var(--accent-soft); color:var(--accent); }}
.hint {{ font-size:12px; color:var(--muted); margin:0; }}
figure {{ margin:24px 0 0; }}
figure img {{ width:100%; border-radius:12px; border:1px solid var(--line); display:block; }}
figcaption {{ font-size:12px; color:var(--muted); margin-top:6px; }}
.tabs {{ display:flex; gap:6px; margin-bottom:10px; }}
.tabs button {{ font:inherit; font-size:14px; padding:7px 12px; border-radius:99px; border:1px solid var(--line);
               background:var(--card); color:var(--ink); cursor:pointer; }}
.tabs button.on {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
@media (prefers-color-scheme: dark) {{ .tabs button.on {{ color:#10241a; }} }}
.missing {{ list-style:none; margin:10px 0 0; padding:0; background:var(--card); border:1px solid var(--line);
           border-radius:14px; overflow:hidden; }}
.missing:empty {{ display:none; }}
.missing li {{ padding:11px 14px; border-top:1px solid var(--line); display:grid;
              grid-template-columns:1fr auto; gap:2px 10px; align-items:baseline; }}
.missing li:first-child {{ border-top:0; }}
.missing .com {{ font-weight:600; }}
.missing .n {{ font-size:13px; color:var(--muted); white-space:nowrap; }}
.missing .sci {{ font-size:13px; color:var(--muted); font-style:italic; }}
.missing code {{ grid-column:1 / -1; font-size:12px; color:var(--accent); user-select:all;
                -webkit-user-select:all; overflow-wrap:anywhere; }}
</style></head><body><main>
<h1>Bird Frame</h1>
<p class="sub">Choose whose birds the frame shows.</p>
{message}
<div class="status" id="status"><span class="dot"></span><span id="statusText">Checking…</span></div>
<div class="list">{locations}</div>
<form method="post" action="/refresh"><button class="secondary" type="submit">Redraw the frame now</button></form>

<h2>Add a location</h2>
<form class="add" method="post" action="/add">
  <label>Name <input name="name" maxlength="60" placeholder="e.g. Mum's place"></label>
  <label>Type
    <select name="kind" id="kind">
      <option value="station">BirdWeather station</option>
      <option value="postcode">Postcode</option>
      <option value="mic">Bird mic / website</option>
    </select></label>
  <div class="row">
    <label id="valueLabel">Station number <input name="value" id="value" required inputmode="numeric" placeholder="30108"></label>
    <label id="countryLabel">Country <input name="country" value="au" maxlength="2"></label>
  </div>
  <p class="hint" id="hint">The number at the end of the station's address on app.birdweather.com.</p>
  <button class="primary" type="submit">Save location</button>
</form>

<h2 id="missingTitle">Heard but not drawn yet</h2>
<div class="tabs" role="group" aria-label="Time period">
  <button type="button" data-days="30" class="on">Last 30 days</button>
  <button type="button" data-days="7">Last 7 days</button>
</div>
<p class="hint" id="missingSummary">Checking BirdWeather…</p>
<ol class="missing" id="missingList"></ol>
{preview}
</main>
<script>
const kinds = {{
  station: ["Station number", "30108", "numeric", "The number at the end of the station's address on app.birdweather.com.", false],
  postcode: ["Postcode", "3000", "text", "Shows the top birds from BirdWeather stations near this postcode.", true],
  mic: ["Address", "http://birdnet.local", "url", "Your own BirdNET mic, or any AvianVisitors site.", false],
}};
const sel = document.getElementById("kind");
function syncKind() {{
  const k = kinds[sel.value], v = document.getElementById("value");
  document.getElementById("valueLabel").firstChild.textContent = k[0] + " ";
  v.placeholder = k[1]; v.inputMode = k[2];
  document.getElementById("hint").textContent = k[3];
  document.getElementById("countryLabel").style.visibility = k[4] ? "visible" : "hidden";
}}
sel.addEventListener("change", syncKind); syncKind();

const box = document.getElementById("status"), text = document.getElementById("statusText");
let wasRunning = false;
async function poll() {{
  try {{
    const s = await (await fetch("/status", {{cache: "no-store"}})).json();
    box.className = "status" + (s.running ? " busy" : s.ok === true ? " ok" : s.ok === false ? " bad" : "");
    if (s.running) text.textContent = "Updating the frame… " + s.seconds + "s (usually 1–3 minutes)";
    else if (s.ok === true) text.textContent = "Frame is up to date.";
    else if (s.ok === false) text.textContent = "Last update didn't finish: " + s.last;
    else text.textContent = "Frame refreshes on its own every 15 minutes when the birds change.";
    if (wasRunning && !s.running) {{ const img = document.getElementById("preview"); if (img) img.src = "/frame.png?t=" + Date.now(); }}
    wasRunning = s.running;
    setTimeout(poll, s.running ? 3000 : 15000);
  }} catch (e) {{ text.textContent = "Can't reach the frame."; setTimeout(poll, 15000); }}
}}
poll();

const mList = document.getElementById("missingList"), mSum = document.getElementById("missingSummary");
let mDays = "30";
function esc(t) {{ const d = document.createElement("div"); d.textContent = t; return d.innerHTML; }}
async function loadMissing() {{
  mSum.textContent = "Checking BirdWeather…"; mList.innerHTML = "";
  try {{
    const r = await (await fetch("/missing?days=" + mDays, {{cache: "no-store"}})).json();
    if (r.error) {{ mSum.textContent = r.error; return; }}
    const left = r.missing.length;
    mSum.textContent = left === 0
      ? "Every bird heard " + (r.place ? "at " + r.place + " " : "") + "in the last " + r.days + " days has a drawing."
      : left + " of " + r.heard + " birds heard at " + r.place + " in the last " + r.days +
        " days " + (left === 1 ? "has" : "have") + " no drawing yet, so the frame skips " +
        (left === 1 ? "it" : "them") + ". Most-heard first; tap a file name to select it.";
    mList.innerHTML = r.missing.map(b =>
      "<li><span class=com>" + esc(b.com) + "</span><span class=n>" + b.n.toLocaleString() +
      "&times;</span><span class=sci>" + esc(b.sci) + "</span><code>" + esc(b.file) + "</code></li>").join("");
  }} catch (e) {{ mSum.textContent = "Can't reach the frame."; }}
}}
document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => {{
  document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("on", x === b));
  mDays = b.dataset.days; loadMissing();
}}));
loadMissing();
</script></body></html>"""


def describe(loc):
    if loc["kind"] == "station":
        return f"BirdWeather station {loc['value']}"
    if loc["kind"] == "postcode":
        return f"Near postcode {loc['value']} ({loc['country'].upper()})"
    return loc["value"]


def render_page(app, message="", error=False):
    cur = current_source(read_config(app.config))
    items = []
    for loc in app.store.load():
        on = is_current(loc, cur)
        e = html.escape
        badge = '<span class="badge">Showing</span>' if on else ""
        items.append(
            f'<div class="loc{" on" if on else ""}">'
            f'<form class="pick" method="post" action="/use"><input type="hidden" name="id" value="{e(loc["id"])}">'
            f'<button type="submit"><b>{e(loc["name"])}{badge}</b><span>{e(describe(loc))}</span></button></form>'
            f'<form class="del" method="post" action="/delete" onsubmit="return confirm(\'Remove {e(loc["name"]).replace(chr(39), "")}?\')">'
            f'<input type="hidden" name="id" value="{e(loc["id"])}"><button type="submit" aria-label="Remove">&times;</button></form>'
            f'</div>')
    if not any(is_current(l, cur) for l in app.store.load()):
        kind, value, _ = cur
        items.insert(0, f'<div class="loc on"><div class="pick"><button disabled><b>Current setting'
                        f'<span class="badge">Showing</span></b><span>{html.escape(str(value))}</span>'
                        f'</button></div></div>')
    msg = f'<div class="msg {"err" if error else "ok"}">{html.escape(message)}</div>' if message else ""
    preview = ('<figure><img id="preview" src="/frame.png" alt="The latest collage the frame drew">'
               '<figcaption>Latest collage (full colour; the screen shows it in six inks)</figcaption></figure>'
               if latest_capture(app) else "")
    return PAGE.format(message=msg, locations="".join(items), preview=preview)


# --- birds heard but not drawn ------------------------------------------------------
MISSING_TTL = 600  # seconds; BirdWeather lookups are cached this long
_missing_cache = {}


def drawn_slugs():
    """File names (without .png) the frame has a picture for, read fresh so
    new drawings show up as soon as build_masks.py has been run."""
    import birdweather
    birdweather._drawable = None  # the project caches this; we want it fresh
    return birdweather.drawable_slugs()


def heard_species(cfg, days):
    """[{sci, com, n}] heard at the frame's current location, unfiltered."""
    import birdweather
    kind, value, country = current_source(cfg)
    if kind == "station":
        return birdweather.top_species_for_station(value, days=days, limit=200), f"station {value}"
    if kind == "postcode":
        lat, lon = birdweather.geocode(value, country or "au")
        # same 15-mile area the frame searches first for a postcode
        return birdweather.top_species(lat, lon, 15, days=days, limit=200), f"postcode {value}"
    return None, None


def missing_birds(app, days):
    cfg = read_config(app.config)
    key = (str(current_source(cfg)), days)
    hit = _missing_cache.get(key)
    if hit and time.time() - hit[0] < MISSING_TTL:
        heard, where = hit[1]
    else:
        heard, where = heard_species(cfg, days)
        if heard is None:
            return {"error": "This list works for BirdWeather stations and postcodes, "
                             "not a bird mic. Your mic's own website lists what it has heard."}
        _missing_cache[key] = (time.time(), (heard, where))
    import birdweather
    have = drawn_slugs()
    missing = []
    for b in heard:
        slug = birdweather.slugify(b["sci"])
        if slug not in have:
            missing.append({"com": b["com"], "sci": b["sci"], "n": int(b.get("n") or 0),
                            "file": slug + ".png"})
    missing.sort(key=lambda b: -b["n"])
    place = str(cfg.get("shoot_location") or "").strip() or where
    return {"place": place, "days": days, "heard": len(heard),
            "drawn": len(heard) - len(missing), "missing": missing}


def latest_capture(app):
    cache = Path(os.path.expanduser(str(read_config(app.config).get("cache", "~/.birdframe"))))
    files = [p for p in (cache / "frame.png", cache / "shot.png") if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


# --- web server -----------------------------------------------------------------
class App:
    def __init__(self, config, store, refresher):
        self.config, self.store, self.refresher = config, store, refresher


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, body, ctype="text/html; charset=utf-8", status=200):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def redirect(self, message="", error=False):
            # Post/Redirect/Get, so reloading the page doesn't repeat the action
            from urllib.parse import urlencode
            query = urlencode({"m": message, **({"e": "1"} if error else {})})
            self.send_response(303)
            self.send_header("Location", "/?" + query)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            parts = urlsplit(self.path)
            path = parts.path
            if path == "/":
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                self.send(render_page(app, q.get("m", "")[:200], q.get("e") == "1"))
            elif path == "/status":
                self.send(json.dumps(app.refresher.status()), "application/json")
            elif path == "/missing":
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                days = 7 if q.get("days") == "7" else 30
                try:
                    result = missing_birds(app, days)
                except Exception as e:  # network trouble, BirdWeather down, etc.
                    result = {"error": f"Couldn't check BirdWeather right now ({e}). Try again shortly."}
                self.send(json.dumps(result), "application/json")
            elif path == "/frame.png":
                img = latest_capture(app)
                if img:
                    self.send(img.read_bytes(), "image/png")
                else:
                    self.send("no image yet", "text/plain", 404)
            else:
                self.send("not found", "text/plain", 404)

        def do_POST(self):
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()}
            path = urlsplit(self.path).path
            with app.store.lock:
                items = app.store.load()
                if path == "/use":
                    loc = next((l for l in items if l["id"] == form.get("id")), None)
                    if not loc:
                        return self.redirect("That location no longer exists.", True)
                    try:
                        write_config(app.config, loc)
                    except Exception as e:  # keep the old config if anything goes wrong
                        return self.redirect(f"Couldn't change the config: {e}", True)
                    app.refresher.start()
                    return self.redirect(f"Switched to {loc['name']}. The frame is redrawing.")
                if path == "/add":
                    loc, err = clean_location(form)
                    if err:
                        return self.redirect(err, True)
                    items.append(loc)
                    app.store.save(items)
                    return self.redirect(f"Saved {loc['name']}. Tap it to show it on the frame.")
                if path == "/delete":
                    app.store.save([l for l in items if l["id"] != form.get("id")])
                    return self.redirect("Removed.")
                if path == "/refresh":
                    app.refresher.start()
                    return self.redirect("Redrawing the frame.")
            self.send("not found", "text/plain", 404)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Web page for switching the bird frame's location.")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    ap.add_argument("--config", default=str(STATE_DIR / "config.toml"))
    args = ap.parse_args()

    config = Path(os.path.expanduser(args.config))
    state = config.parent
    state.mkdir(parents=True, exist_ok=True)
    store = Store(state / "locations.json")
    if not store.path.exists():
        store.save(store.load())
    app = App(config, store, Refresher(config, state / "switcher-refresh.log"))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"Location switcher running on port {args.port} (config: {config})")
    server.serve_forever()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
mimir web UI - view recordings, live level meter, adjust settings
"""

import json
import os
import numpy as np
import subprocess
import time
from pathlib import Path
from datetime import datetime
import socket as socket_lib
from flask import Flask, render_template_string, jsonify, request, send_file, redirect, url_for, Response, stream_with_context, abort, make_response
from functools import wraps

CONFIG_PATH = Path(__file__).parent / "config.json"
STATE_PATH = Path("/run/mimir/state.json")
LIVE_SOCKET = Path("/run/mimir/live.sock")

app = Flask(__name__)

# Background CPU monitor — updates every 2s instead of blocking each request
_cpu_cache = {"pct": 0, "temp": 0}
def _cpu_monitor():
    import time as _t
    prev_idle, prev_total = 0, 0
    while True:
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()
            vals = list(map(int, parts[1:]))
            idle = vals[3] + vals[4]
            total = sum(vals)
            if prev_total:
                dt = total - prev_total
                di = idle - prev_idle
                _cpu_cache["pct"] = round((1 - di / dt) * 100) if dt else 0
            prev_idle, prev_total = idle, total
        except Exception:
            pass
        try:
            _cpu_cache["temp"] = round(float(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000, 1)
        except Exception:
            pass
        _t.sleep(2)

import threading as _thr
_thr.Thread(target=_cpu_monitor, daemon=True).start()
app.secret_key = "mimir-session-key-2026"


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        cfg = load_config()
        pin = cfg.get("ui_pin", "")
        if not pin:
            return f(*args, **kwargs)  # no pin set, open access
        token = request.cookies.get("mimir_auth", "")
        if token != pin:
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    cfg = load_config()
    pin = cfg.get("ui_pin", "")
    if not pin:
        return redirect("/")
    error = ""
    if request.method == "POST":
        entered = request.form.get("pin", "")
        if entered == pin:
            resp = make_response(redirect("/"))
            resp.set_cookie("mimir_auth", pin, max_age=60*60*24*90, httponly=True)
            return resp
        error = "Wrong PIN"
    return f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>mimir — login</title>
<style>body{{font-family:"SF Mono",monospace;background:#0d1117;color:#c9d1d9;display:flex;align-items:center;justify-content:center;min-height:100vh}}
.box{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;max-width:300px;width:100%;text-align:center}}
h1{{color:#58a6ff;font-size:1.2rem;margin-bottom:16px}}
input{{background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:10px;font-family:inherit;font-size:1.1rem;text-align:center;width:100%;letter-spacing:4px;margin-bottom:12px}}
button{{background:#1f6feb;border:none;color:white;padding:10px 24px;border-radius:6px;cursor:pointer;font-family:inherit;font-size:0.9rem;width:100%}}
.err{{color:#f85149;font-size:0.85rem;margin-bottom:8px}}
</style></head><body>
<div class="box"><h1>mimir</h1>
{"<div class=err>" + error + "</div>" if error else ""}
<form method="POST"><input type="password" name="pin" placeholder="PIN" autofocus>
<button type="submit">Enter</button></form></div></body></html>'''



def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"state": "unknown", "rms": 0, "threshold": 0, "total_events": 0,
                "last_event": None, "calibrating": False, "ts": 0}


LABEL_ICONS = {
    "noise": "〰", "rain": "🌧", "wind": "💨", "speech": "🗣",
    "bird": "🐦", "aircraft": "✈", "helicopter": "🚁", "vehicle": "🚗",
    # Corvids & common Pacific NW species
    "american crow":     "🐦‍⬛",
    "common raven":      "🪶",
    "steller's jay":     "🔵",
    "blue jay":          "🔵",
    "fish crow":         "🐦‍⬛",
    "northwestern crow": "🐦‍⬛",
    "clark's nutcracker":"🤍",
    # Other common species
    "black-capped chickadee": "🐦",
    "american robin":    "🐦",
    "song sparrow":      "🐦",
    "dark-eyed junco":   "🐦",
    "house sparrow":     "🐦",
    "european starling": "🐦",
    "red-tailed hawk":   "🦅",
    "bald eagle":        "🦅",
    "great horned owl":  "🦉",
    "barred owl":        "🦉",
}
KNOWN_LABELS = list(LABEL_ICONS.keys())

CORVIDS = {"american crow", "common raven", "northwestern crow",
           "fish crow", "steller's jay", "blue jay", "clark's nutcracker"}
RAPTORS = {"red-tailed hawk", "bald eagle", "cooper's hawk", "sharp-shinned hawk",
           "peregrine falcon", "merlin", "northern harrier", "osprey"}
OWLS = {"great horned owl", "barred owl", "short-eared owl", "northern saw-whet owl",
        "western screech-owl", "snowy owl"}
SOUND_TAGS = {"noise", "rain", "wind", "speech", "aircraft", "helicopter", "vehicle", "music"}


_species_cache = {"ts": 0, "species": [], "sounds": []}

def get_detected_species():
    """Scan sidecars for all unique bird species and sound tags detected. Cached 60s."""
    now = time.time()
    if now - _species_cache["ts"] < 60:
        return _species_cache["species"], _species_cache["sounds"]
    cfg = load_config()
    rdir = Path(cfg["recordings_dir"])
    species = set()
    sounds = set()
    for sidecar in rdir.rglob("*.json"):
        try:
            d = json.loads(sidecar.read_text())
            if d.get("status") != "done": continue
            for t in d.get("tags", []):
                label = t["label"]
                if label in SOUND_TAGS:
                    sounds.add(label)
            for b in d.get("birds", []):
                species.add(b["label"])
        except:
            pass
    _species_cache["ts"] = now
    _species_cache["species"] = sorted(species)
    _species_cache["sounds"] = sorted(sounds)
    return _species_cache["species"], _species_cache["sounds"]


_bird_counts_cache = {"ts": 0, "counts": {}, "corvid": 0}

def get_today_bird_counts():
    """Return {label: count} for birds detected today, and total crow-family count. Cached 30s."""
    now = time.time()
    if now - _bird_counts_cache["ts"] < 30:
        return _bird_counts_cache["counts"], _bird_counts_cache["corvid"]
    from datetime import datetime
    from collections import defaultdict
    cfg = load_config()
    rdir = Path(cfg["recordings_dir"])
    today = datetime.now().strftime("%Y-%m-%d")
    today_dir = rdir / today
    counts = defaultdict(int)
    # Only scan today's directory, not all recordings
    if today_dir.exists():
        for sidecar in today_dir.glob("*.json"):
            try:
                d = json.loads(sidecar.read_text())
                if d.get("status") != "done": continue
                for b in d.get("birds", []):
                    counts[b["label"]] += 1
            except Exception:
                pass
    corvid_total = sum(v for k, v in counts.items()
                       if k in CORVIDS)
    _bird_counts_cache["ts"] = now
    _bird_counts_cache["counts"] = dict(counts)
    _bird_counts_cache["corvid"] = corvid_total
    return dict(counts), corvid_total

def get_recordings(tag_filter=None):
    import wave as wavelib
    cfg = load_config()
    rdir = Path(cfg["recordings_dir"])
    recordings = []
    for wav in sorted(rdir.rglob("*.wav"), key=lambda f: f.stat().st_mtime, reverse=True):
        stat = wav.stat()
        duration = "—"
        try:
            with wavelib.open(str(wav), "r") as wf:
                secs = wf.getnframes() / wf.getframerate()
                m, s = int(secs // 60), int(secs % 60)
                duration = f"{m}:{s:02d}"
        except Exception:
            pass
        # Load analysis sidecar if present
        analysis = {}
        sidecar = wav.with_suffix(".json")
        if sidecar.exists():
            try:
                analysis = json.loads(sidecar.read_text())
            except Exception:
                pass
        feedback = analysis.get("feedback", {})
        wrong_tags = set(feedback.get("wrong", []))
        tags = [t for t in analysis.get("tags", []) if t.get("source") != "birdnet"]
        from datetime import datetime as _dt
        _now = _dt.now()
        _mt = _dt.fromtimestamp(stat.st_mtime)
        if _mt.date() == _now.date():
            _day = "Today"
        elif (_now.date() - _mt.date()).days == 1:
            _day = "Yesterday"
        else:
            _day = _mt.strftime("%a %b %-d")
        display_time = f"{_day} {_mt.strftime('%-I:%M %p')}"
        recordings.append({
            "path": str(wav).lstrip("/"),
            "name": wav.name,
            "display_time": display_time,
            "date": wav.parent.name,
            "size_kb": round(stat.st_size / 1024, 1),
            "mtime": stat.st_mtime,
            "duration": duration,
            "tags": tags,
            "wrong_tags": wrong_tags,
            "transcript": analysis.get("transcript", None),
            "analysis_status": analysis.get("status", None),
            "photo": analysis.get("photo", None),
            "video": analysis.get("video", None),
            "birds": analysis.get("birds", []),
        })

    # Apply filter
    if tag_filter and tag_filter != "all":
        def _all_labels(r):
            return {t["label"] for t in r["tags"]} | {b["label"] for b in r.get("birds", [])}
        if tag_filter == "interesting":
            recordings = [r for r in recordings
                          if any(t["label"] != "noise" for t in r["tags"]) or r.get("birds")]
        elif tag_filter == "corvid":
            recordings = [r for r in recordings if _all_labels(r) & CORVIDS]
        elif tag_filter == "raptor":
            recordings = [r for r in recordings if _all_labels(r) & RAPTORS]
        elif tag_filter == "owl":
            recordings = [r for r in recordings if _all_labels(r) & OWLS]
        elif tag_filter == "untagged":
            recordings = [r for r in recordings if not r["tags"] and not r.get("birds")]
        else:
            recordings = [r for r in recordings if tag_filter in _all_labels(r)]

    return recordings


TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mimir — sound monitor</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', monospace; background: #0d1117; color: #c9d1d9; min-height: 100vh; }
  header { background: #161b22; border-bottom: 1px solid #30363d; padding: 16px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 1.2rem; color: #58a6ff; }
  header .status { font-size: 0.8rem; padding: 3px 10px; border-radius: 12px; }
  .status.idle { background: #1f6feb22; color: #58a6ff; border: 1px solid #1f6feb; }
  .status.recording { background: #da363322; color: #f85149; border: 1px solid #da3633; animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.5; } }
  .container { max-width: 900px; margin: 0 auto; padding: 24px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; }
  .card h2 { font-size: 0.75rem; text-transform: uppercase; color: #8b949e; letter-spacing: 1px; margin-bottom: 12px; }
  .big-num { font-size: 2rem; color: #e6edf3; font-weight: 600; }
  .meter-wrap { margin: 12px 0; }
  .meter-bg { background: #21262d; border-radius: 4px; height: 12px; overflow: hidden; }
  .meter-fill { height: 100%; border-radius: 4px; transition: width 0.1s ease; background: linear-gradient(90deg, #238636, #d29922, #da3633); }
  .threshold-line { position: relative; }
  .settings-form { display: grid; gap: 12px; }
  .field { display: flex; flex-direction: column; gap: 4px; }
  .field label { font-size: 0.8rem; color: #8b949e; }
  .field input[type=number], .field input[type=range] { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #e6edf3; padding: 6px 10px; font-family: inherit; }
  .field input[type=range] { padding: 4px 0; cursor: pointer; }
  .val-display { font-size: 0.85rem; color: #58a6ff; }
  button { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-family: inherit; font-size: 0.85rem; }
  button:hover { background: #30363d; }
  button.primary { background: #1f6feb; border-color: #1f6feb; color: white; }
  button.primary:hover { background: #388bfd; }
  button.danger { background: #da363322; border-color: #da3633; color: #f85149; }
  .recordings-list { display: grid; gap: 8px; }
  .rec-item { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 12px; display: flex; align-items: center; gap: 12px; }
  .rec-player { flex: 1; min-width: 0; }
  .waveform-wrap { position: relative; cursor: pointer; margin: 6px 0 4px; }
  .waveform-canvas { width: 100%; height: 52px; display: block; border-radius: 4px; background: #0a0e14; }
  .playhead { position: absolute; top: 0; bottom: 0; width: 2px; background: #58a6ff; pointer-events: none; opacity: 0; transition: opacity 0.1s; }
  .player-controls { display: flex; align-items: center; gap: 8px; }
  .play-btn, .stop-btn { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; width: 28px; height: 28px; border-radius: 50%; cursor: pointer; font-size: 0.7rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
  .play-btn:hover, .stop-btn:hover { background: #30363d; }
  .time-display { font-size: 0.72rem; color: #8b949e; white-space: nowrap; }
  .rec-meta { font-size: 0.75rem; color: #8b949e; min-width: 140px; }
  .rec-name { font-size: 0.85rem; color: #e6edf3; font-weight: 600; }
  .rec-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; align-items: center; }
  .tag { font-size: 0.72rem; background: #21262d; border: 1px solid #30363d; border-radius: 10px; padding: 1px 4px 1px 7px; color: #c9d1d9; white-space: nowrap; display: inline-flex; align-items: center; gap: 3px; }
  .tag.wrong { opacity: 0.45; text-decoration: line-through; border-color: #da3633; }
  .tag.manual { border-color: #238636; color: #3fb950; }
  .tag-conf { color: #8b949e; }
  .tag-x { background: none; border: none; color: #8b949e; cursor: pointer; font-size: 0.65rem; padding: 0 1px; line-height: 1; }
  .tag-x:hover { color: #f85149; }
  .tag-add { font-size: 0.72rem; background: none; border: 1px dashed #30363d; border-radius: 10px; padding: 1px 7px; color: #8b949e; cursor: pointer; white-space: nowrap; }
  .tag-add:hover { border-color: #58a6ff; color: #58a6ff; }
  .rec-transcript { font-size: 0.78rem; color: #8b949e; margin-top: 4px; font-style: italic; line-height: 1.4; }
  .rec-birds { margin-top: 5px; display: flex; flex-wrap: wrap; gap: 6px; }
  .bird-det { font-size: 0.78rem; background: #0d1f0d; border: 1px solid #238636; border-radius: 8px; padding: 2px 8px; color: #3fb950; }
  .bird-img-wrap { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 8px; }
  .bird-img-card { background: #0d1f0d; border: 1px solid #238636; border-radius: 8px; overflow: hidden; max-width: 120px; cursor: zoom-in; transition: max-width 0.25s; }
  .bird-img-card.expanded { max-width: 100%; cursor: zoom-out; }
  .bird-img-card img { width: 100%; display: block; border-radius: 6px 6px 0 0; }
  .bird-img-card .bird-img-label { font-size: 0.7rem; color: #3fb950; padding: 3px 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .rec-photo { margin-top: 8px; }
  .rec-photo img { width: 100%; max-width: 320px; border-radius: 6px; border: 1px solid #30363d; cursor: zoom-in; transition: max-width 0.2s; }
  .rec-photo video { max-height: 240px; }
  .rec-photo img.expanded { max-width: 100%; cursor: zoom-out; }
  .section-title { font-size: 0.75rem; text-transform: uppercase; color: #8b949e; letter-spacing: 1px; margin: 24px 0 12px; }
  .alert { background: #d2992222; border: 1px solid #d29922; border-radius: 6px; padding: 10px 14px; font-size: 0.85rem; color: #d29922; margin-bottom: 16px; }
  .full-width { grid-column: 1 / -1; }

  /* ── Mobile / iPhone ─────────────────────────────── */
  @media (max-width: 640px) {
    header { flex-wrap: wrap; gap: 8px; padding: 12px 16px; }
    header h1 { font-size: 1rem; }
    header .status { font-size: 0.72rem; }
    .header-btns { display: flex; flex-wrap: wrap; gap: 6px; width: 100%; }
    .container { padding: 12px; }
    .grid { grid-template-columns: 1fr; }
    .rec-item { flex-direction: column; align-items: stretch; gap: 8px; }
    .big-num { font-size: 1.5rem; }
    .fbtn { font-size: 0.7rem; padding: 3px 8px; }
    details > summary { cursor: pointer; list-style: none; display: flex; align-items: center; justify-content: space-between; }
    details > summary::after { content: " ▶"; font-size: 0.7rem; color: #8b949e; }
    details[open] > summary::after { content: " ▼"; }
    details > summary h2 { pointer-events: none; }
  }
</style>
</head>
<body>
<header>
  <h1>🦉 mimir</h1>
  <span class="status {{ state.state }}" id="status-badge">{{ state.state }}</span>
  <span id="events-count" style="margin-left:auto;font-size:0.8rem;color:#8b949e">{{ state.total_events }} events recorded</span>
  <span id="queue-indicator" style="display:none;font-size:0.78rem;color:#f0883e;background:#f0883e18;border:1px solid #f0883e55;border-radius:10px;padding:2px 10px">⏳ <span id="queue-label">analyzing…</span></span>
  <span id="cpu-indicator" style="font-size:0.78rem;color:#8b949e;background:#21262d;border:1px solid #30363d;border-radius:10px;padding:2px 10px;white-space:nowrap">CPU <span id="cpu-pct">—</span></span>
  <button id="monitor-btn" onclick="toggleMonitoring()" data-enabled="{{ 'true' if cfg.get('monitoring_enabled', True) else 'false' }}"
    style="padding:3px 14px;font-size:0.8rem;border-radius:12px;cursor:pointer;
    {% if cfg.get('monitoring_enabled', True) %}border:1px solid #da3633;background:#da363322;color:#f85149{% else %}border:1px solid #238636;background:#23863622;color:#3fb950{% endif %}">
    {{ '⏹ Stop' if cfg.get('monitoring_enabled', True) else '⏺ Start' }}
  </button>
  <button id="mode-btn" onclick="toggleMode()" data-mode="{{ cfg.get('mode','event') }}"
    style="padding:3px 14px;font-size:0.8rem;border-radius:12px;cursor:pointer;
    {% if cfg.get('mode','event') == 'continuous' %}border:1px solid #238636;background:#23863622;color:#3fb950{% else %}border:1px solid #30363d;background:#21262d;color:#c9d1d9{% endif %}">
    {{ '⏺ Continuous' if cfg.get('mode','event') == 'continuous' else '⚡ Event' }}
  </button>
  <div class="header-btns" style="display:contents">
  <button id="live-btn" onclick="toggleLive()" style="padding:3px 14px;font-size:0.8rem;border-radius:12px;border:1px solid #30363d;background:#21262d;color:#c9d1d9;cursor:pointer">🎤 Live</button>
  <a href="/stats" style="padding:3px 14px;font-size:0.8rem;border-radius:12px;border:1px solid #30363d;background:#21262d;color:#c9d1d9;text-decoration:none;cursor:pointer">📊 Stats</a>
  <a href="/crows" style="padding:3px 14px;font-size:0.8rem;border-radius:12px;border:1px solid #30363d;background:#21262d;color:#c9d1d9;text-decoration:none;cursor:pointer">🐦‍⬛ Corvids</a>
  <a href="/camera_feed" style="padding:3px 14px;font-size:0.8rem;border-radius:12px;border:1px solid #30363d;background:#21262d;color:#c9d1d9;text-decoration:none;cursor:pointer">📷 Camera</a>
  </div>
</header>
<div class="container">

<div class="alert" id="cal-alert" style="display:none">
  Calibrating baseline — stay quiet... <strong><span id="cal-countdown">10</span>s</strong> remaining.
</div>

<div class="grid">
  <div class="card">
    <h2>Live Level</h2>
    <div class="meter-wrap">
      <div style="display:flex;justify-content:space-between;font-size:0.75rem;color:#8b949e;margin-bottom:4px">
        <span>0</span><span id="rms-val">{{ "%.4f"|format(state.rms) }}</span><span>threshold: <span id="thr-val">{{ "%.4f"|format(state.threshold) }}</span></span>
      </div>
      <div class="meter-bg">
        <div class="meter-fill" id="meter-fill" style="width:{{ [state.rms / (state.threshold * 2) * 100, 100]|min }}%"></div>
      </div>
    </div>
    <div style="font-size:0.8rem;color:#8b949e;margin-top:8px">
      baseline: <span id="baseline-val">{{ "%.4f"|format(state.baseline_rms) }}</span> &nbsp;|&nbsp;
      multiplier: <span id="mult-val">{{ state.threshold_multiplier }}x</span>
    </div>
  </div>

  <div class="card">
    <h2>Today's Birds</h2>
    <div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:8px">
      <div>
        <div class="big-num" style="color:#3fb950" id="corvid-count">{{ corvid_total }}</div>
        <div style="font-size:0.72rem;color:#8b949e">corvid calls</div>
      </div>
      <div style="border-left:1px solid #30363d;padding-left:12px">
        <div class="big-num" style="font-size:1.4rem">{{ state.total_events }}</div>
        <div style="font-size:0.72rem;color:#8b949e">total events</div>
      </div>
    </div>
    {% if bird_counts %}
    <div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">
      {% for label, count in bird_counts|dictsort(by='value', reverse=true) %}
      <span style="font-size:0.72rem;background:#0d1f0d;border:1px solid #238636;border-radius:10px;padding:1px 8px;color:#3fb950">
        {{ label }} <strong>{{ count }}</strong>
      </span>
      {% endfor %}
    </div>
    {% else %}
    <div style="font-size:0.82rem;color:#8b949e;margin-top:4px">No birds detected yet today</div>
    {% endif %}
    {% if state.last_event %}
    <div style="font-size:0.75rem;color:#8b949e;margin-top:8px">last event {{ state.last_event.time[11:19] }} · {{ state.last_event.duration }}s</div>
    {% endif %}
  </div>
</div>

<div class="grid">
  <div class="card" style="padding:0">
    <details>
    <summary style="padding:20px"><h2 style="display:inline">Settings</h2></summary>
    <div style="padding:0 20px 20px">
    <form class="settings-form" method="POST" action="/settings">
      <div class="field">
        <label>Threshold multiplier (× baseline)</label>
        <input type="range" name="threshold_multiplier" min="1.2" max="10" step="0.1"
               value="{{ cfg.threshold_multiplier }}"
               oninput="this.nextElementSibling.textContent=this.value+'×'">
        <span class="val-display">{{ cfg.threshold_multiplier }}×</span>
      </div>
      <div class="field">
        <label>Pre-roll seconds</label>
        <input type="number" name="pre_roll_seconds" value="{{ cfg.pre_roll_seconds }}" min="0" max="10" step="0.5">
      </div>
      <div class="field">
        <label>Post-roll seconds</label>
        <input type="number" name="post_roll_seconds" value="{{ cfg.post_roll_seconds }}" min="0.5" max="30" step="0.5">
      </div>
      <div class="field">
        <label>Max recording duration (seconds)</label>
        <input type="number" name="max_duration_seconds" value="{{ cfg.max_duration_seconds }}" min="5" max="300" step="5">
      </div>
      <div class="field">
        <label>Continuous chunk duration (seconds)</label>
        <input type="number" name="continuous_chunk_seconds" value="{{ cfg.get('continuous_chunk_seconds', 300) }}" min="30" max="3600" step="30">
      </div>
      <div class="field">
        <label style="flex-direction:row;align-items:center;gap:8px;cursor:pointer">
          <input type="checkbox" name="round_robin" {% if cfg.get('round_robin') %}checked{% endif %} style="width:auto">
          Round-robin (auto-delete oldest when full)
        </label>
      </div>
      <div class="field">
        <label>Max recordings size (GB)</label>
        <input type="number" name="max_recordings_gb" value="{{ cfg.get('max_recordings_gb', 10) }}" min="0.1" max="2000" step="0.5">
      </div>
      <div class="field">
        <label style="flex-direction:row;align-items:center;gap:8px;cursor:pointer">
          <input type="checkbox" name="analysis_enabled" {% if cfg.get('analysis_enabled', True) %}checked{% endif %} style="width:auto">
          Auto-analyze recordings (tags + classification)
        </label>
      </div>
      <div class="field">
        <label style="flex-direction:row;align-items:center;gap:8px;cursor:pointer">
          <input type="checkbox" name="whisper_enabled" {% if cfg.get('whisper_enabled') %}checked{% endif %} style="width:auto">
          Whisper speech transcription (slow on Pi)
        </label>
      </div>
      <div class="field">
        <label>Whisper model size</label>
        <select name="whisper_model" style="background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:6px 10px;font-family:inherit">
          {% for m in ['tiny', 'tiny.en', 'base', 'base.en', 'small'] %}
          <option value="{{ m }}" {% if cfg.get('whisper_model','tiny') == m %}selected{% endif %}>{{ m }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="field">
        <label>UI PIN (leave blank for open access)</label>
        <input type="password" name="ui_pin" value="{{ cfg.get('ui_pin','') }}" placeholder="e.g. 1234" style="background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:6px 10px;font-family:inherit">
        <span style="font-size:0.72rem;color:#8b949e">Protects dashboard, settings, and non-bird clips. Bird feed (/birds) stays public.</span>
      </div>
      <div class="field">
        <label>Push notifications (ntfy.sh topic)</label>
        <input type="text" name="ntfy_topic" value="{{ cfg.get('ntfy_topic','') }}" placeholder="e.g. mimir-brandon-birds" style="background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:6px 10px;font-family:inherit">
        <span style="font-size:0.72rem;color:#8b949e">Install ntfy app → subscribe to your topic → get crow alerts</span>
      </div>
      <div class="field">
        <label>Notify on species (comma-separated, blank = all birds)</label>
        <input type="text" name="ntfy_species" value="{{ cfg.get('ntfy_species','american crow,common raven') }}" style="background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:6px 10px;font-family:inherit">
      </div>
      <div class="field">
        <label>Local URL (for share links on local WiFi)</label>
        <input type="text" name="local_url" value="{{ cfg.get('local_url','') }}" placeholder="http://10.0.0.9:8765" style="background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:6px 10px;font-family:inherit">
      </div>
      <div class="field">
        <label>Tailscale URL (for remote share links + ntfy)</label>
        <input type="text" name="tailscale_url" value="{{ cfg.get('tailscale_url','') }}" placeholder="http://mimir-1.xxx.ts.net:8765" style="background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:6px 10px;font-family:inherit">
      </div>
      <button type="submit" class="primary">Save Settings</button>
    </form>
    </div>
    </details>
  </div>

  <div class="card" style="padding:0">
    <details>
    <summary style="padding:20px"><h2 style="display:inline">Baseline Calibration</h2></summary>
    <div style="padding:0 20px 20px">
    <p style="font-size:0.85rem;color:#8b949e;margin-bottom:12px">
      Measures ambient noise for 10 seconds to set the detection baseline.
      Make sure it's quiet (no talking, no loud sounds).
    </p>
    <p style="font-size:0.85rem;margin-bottom:16px">
      Current baseline: <strong style="color:#58a6ff">{{ "%.5f"|format(cfg.baseline_rms) }}</strong>
    </p>
    <form method="POST" action="/calibrate">
      <button type="submit" class="danger">Start Calibration (10s)</button>
    </form>
    </div>
    </details>
  </div>
</div>

<div class="card" style="padding:0;margin-bottom:16px">
  <details id="storage-details">
  <summary style="padding:14px 20px;display:flex;align-items:center;gap:12px;cursor:pointer;list-style:none">
    <h2 style="display:inline;margin:0">Storage</h2>
    <span id="disk-summary" style="font-size:0.8rem;color:#8b949e;flex:1">loading…</span>
    <div style="flex:1;max-width:120px">
      <div class="meter-bg" style="height:8px">
        <div id="disk-bar-mini" class="meter-fill" style="width:0%;height:8px;background:linear-gradient(90deg,#238636,#d29922,#da3633)"></div>
      </div>
    </div>
    <span style="font-size:0.7rem;color:#8b949e">▶</span>
  </summary>
  <div style="padding:0 20px 20px">
  <div style="display:flex;gap:32px;flex-wrap:wrap;margin-bottom:10px">
    <div>
      <div style="font-size:0.75rem;color:#8b949e;margin-bottom:2px">Recordings</div>
      <div id="disk-rec-size" style="font-size:1.1rem;color:#e6edf3">—</div>
    </div>
    <div>
      <div style="font-size:0.75rem;color:#8b949e;margin-bottom:2px">Device Free</div>
      <div id="disk-free" style="font-size:1.1rem;color:#e6edf3">—</div>
    </div>
    <div>
      <div style="font-size:0.75rem;color:#8b949e;margin-bottom:2px">Mount</div>
      <div id="disk-mount" style="font-size:0.85rem;color:#8b949e;font-family:monospace">—</div>
    </div>
    <div>
      <div style="font-size:0.75rem;color:#8b949e;margin-bottom:2px">Round-robin limit</div>
      <div id="disk-limit" style="font-size:1.1rem;color:#e6edf3">{{ cfg.get('max_recordings_gb', 10) }} GB{% if not cfg.get('round_robin') %} <span style="font-size:0.75rem;color:#8b949e">(off)</span>{% endif %}</div>
    </div>
  </div>
  <div class="meter-bg">
    <div id="disk-bar" class="meter-fill" style="width:0%;background:linear-gradient(90deg,#238636,#d29922,#da3633)"></div>
  </div>
  <div id="disk-sd-warn" style="display:none;margin-top:10px;background:#d2992222;border:1px solid #d29922;border-radius:6px;padding:8px 12px;font-size:0.82rem;color:#d29922">
    ⚠ Recordings are on the SD card — this causes heavy write wear.
    Mount a USB drive and set <code style="background:#0d1117;padding:1px 4px;border-radius:3px">recordings_dir</code> in config.json to the external path.
  </div>
  </div>
  </details>
</div>

<div style="display:flex;align-items:center;justify-content:space-between;margin:24px 0 8px">
  <div class="section-title" style="margin:0">Recordings (<span id="total-count">{{ total }}</span>)</div>
  <form method="POST" action="/delete_all" onsubmit="return confirm('Delete all recordings?')">
    <button type="submit" class="danger" style="font-size:0.75rem;padding:4px 10px">Delete All</button>
  </form>
</div>
<div style="margin-bottom:10px;display:flex;align-items:center;gap:10px">
  <label style="font-size:0.75rem;color:#8b949e;white-space:nowrap">Filter:</label>
  <select id="filter-select" onchange="setFilter(this.value)"
    style="background:#161b22;border:1px solid #30363d;border-radius:8px;color:#e6edf3;padding:5px 10px;font-family:inherit;font-size:0.85rem;cursor:pointer;flex:1;max-width:260px">
    <option value="all" {% if tag_filter=='all' %}selected{% endif %}>All recordings</option>
    <option value="interesting" {% if tag_filter=='interesting' %}selected{% endif %}>★ Interesting</option>
    <option value="untagged" {% if tag_filter=='untagged' %}selected{% endif %}>? Untagged</option>
    <optgroup label="─ Groups">
      <option value="corvid" {% if tag_filter=='corvid' %}selected{% endif %}>🐦‍⬛ Corvids</option>
      <option value="raptor" {% if tag_filter=='raptor' %}selected{% endif %}>🦅 Raptors</option>
      <option value="owl" {% if tag_filter=='owl' %}selected{% endif %}>🦉 Owls</option>
    </optgroup>
    {% if detected_species %}
    <optgroup label="─ Species detected">
      {% for sp in detected_species %}
      <option value="{{ sp }}" {% if tag_filter==sp %}selected{% endif %}>{{ label_icons.get(sp, '🐦') }} {{ sp }}</option>
      {% endfor %}
    </optgroup>
    {% endif %}
    {% if detected_sounds %}
    <optgroup label="─ Sound types">
      {% for s in detected_sounds %}
      <option value="{{ s }}" {% if tag_filter==s %}selected{% endif %}>{{ label_icons.get(s, '🔊') }} {{ s }}</option>
      {% endfor %}
    </optgroup>
    {% endif %}
  </select>
</div>
<div id="pagination-top"></div>
<div class="recordings-list">
{% for rec in recordings %}
<div class="rec-item" data-path="{{ rec.path }}">
  <div class="rec-player">
    <div style="display:flex;justify-content:space-between;align-items:baseline">
      <div class="rec-name">{{ rec.display_time }}</div>
      <div class="rec-meta">{{ rec.duration }} &nbsp;{{ rec.size_kb }} KB</div>
    </div>
    <div class="waveform-wrap" onclick="seekAudio(event)">
      <canvas class="waveform-canvas" data-audiopath="/audio/{{ rec.path | urlencode }}"></canvas>
      <div class="playhead"></div>
    </div>
    <div class="player-controls">
      <button class="play-btn" onclick="togglePlay(this)">&#9654;</button>
      <button class="stop-btn" onclick="stopAudio(this)" title="Stop">&#9632;</button>
      <span class="time-display">0:00 / {{ rec.duration if rec.duration is defined else '&mdash;' }}</span>
    </div>
    <div class="rec-tags">
      {% for tag in rec.tags %}
      <span class="tag {% if tag.label in rec.wrong_tags %}wrong{% endif %}{% if tag.manual is defined and tag.manual %} manual{% endif %}">
        {{ tag.icon }} {{ tag.label }}{% if not tag.manual is defined or not tag.manual %} <span class="tag-conf">{{ (tag.confidence * 100)|int }}%</span>{% endif %}
        {% if tag.label in rec.wrong_tags %}
        <button class="tag-x" onclick="feedbackTag(event,'{{ rec.path }}','unwrong','{{ tag.label }}')" title="Restore">↩</button>
        {% else %}
        <button class="tag-x" onclick="feedbackTag(event,'{{ rec.path }}','wrong','{{ tag.label }}')" title="Mark wrong">✕</button>
        {% endif %}
      </span>
      {% endfor %}
      {% if rec.analysis_status %}
      <button class="tag-add" onclick="showAddTag(this,'{{ rec.path }}')">+ tag</button>
      {% endif %}
      
    </div>
    {% if rec.birds %}
    <div class="rec-birds">
      {% for b in rec.birds %}
      {% if b.start is defined %}<span class="bird-det" style="cursor:pointer" onclick="seekAndPlay(this, {{ b.start }})" title="Jump to {{ b.start }}s">{{ b.icon }} <strong>{{ b.label }}</strong> <span class="tag-conf">{{ (b.confidence*100)|int }}%</span> <span style="color:#8b949e;font-size:0.7rem">▶ {{ b.start }}–{{ b.end }}s</span></span>{% else %}<span class="bird-det">{{ b.icon }} <strong>{{ b.label }}</strong> <span class="tag-conf">{{ (b.confidence*100)|int }}%</span></span>{% endif %}{% if b.crow_name is defined %} <span style="font-size:0.72rem;background:#1a1a2e;border:1px solid #6f42c1;border-radius:8px;padding:1px 7px;color:#d2a8ff">{{ b.crow_name }}{% if b.is_new_crow %} ✨{% endif %} <span style="color:#8b949e">#{{ b.crow_sightings }}</span></span>{% endif %} <button onclick="confirmBird(event,'{{ rec.path | e }}','{{ b.label | e }}',true)" style="background:#23863622;border:1px solid #238636;border-radius:4px;color:#3fb950;font-size:0.7rem;padding:1px 5px;cursor:pointer" title="Confirm">✓</button><button onclick="confirmBird(event,'{{ rec.path | e }}','{{ b.label | e }}',false)" style="background:#da363311;border:1px solid #da3633;border-radius:4px;color:#f85149;font-size:0.7rem;padding:1px 5px;cursor:pointer;margin-left:2px" title="Wrong">✗</button>
      {% endfor %}
    </div>
    {% set birds_with_images = rec.birds | selectattr('image_url', 'defined') | list %}
    {% if birds_with_images %}
    <div class="bird-img-wrap">
      {% for b in birds_with_images %}
      <div class="bird-img-card" onclick="this.classList.toggle('expanded')" title="{{ b.label }}">
        <img src="{{ b.image_url }}" alt="{{ b.label }}" loading="lazy">
        <div class="bird-img-label">{{ b.icon }} {{ b.label }} {{ (b.confidence*100)|int }}%</div>
      </div>
      {% endfor %}
    </div>
    {% endif %}
    {% endif %}
    {% if rec.transcript and rec.transcript.text %}
    <div class="rec-transcript">{{ rec.transcript.text }}</div>
    {% endif %}
    {% if rec.video %}
    <div class="rec-photo">
      <video src="/api/camera/photo/{{ rec.video }}" controls preload="none" poster="{% if rec.photo %}/api/camera/photo/{{ rec.photo }}{% endif %}"
             style="width:100%;max-width:400px;border-radius:6px;border:1px solid #30363d"></video>
    </div>
    {% elif rec.photo %}
    <div class="rec-photo">
      <img src="/api/camera/photo/{{ rec.photo }}" alt="captured" loading="lazy"
           onclick="this.classList.toggle('expanded')" title="Click to expand">
    </div>
    {% endif %}
    <audio src="/audio/{{ rec.path | urlencode }}" preload="none"></audio>
  </div>
  <button onclick="shareLAN('{{ rec.name | e }}')" style="margin-left:4px;padding:4px 6px;font-size:0.7rem;color:#3fb950;border:1px solid #238636;background:#23863611;border-radius:6px;cursor:pointer;flex-shrink:0" title="Copy LAN link (friends)">&#x1F517; LAN</button>
  <button onclick="shareTS('{{ rec.name | e }}')" style="margin-left:2px;padding:4px 6px;font-size:0.7rem;color:#58a6ff;border:1px solid #1f6feb;background:#1f6feb11;border-radius:6px;cursor:pointer;flex-shrink:0" title="Copy Tailscale link">&#x1F517; TS</button>
  <button onclick="deleteRec(this, '{{ rec.path | e }}')" style="margin-left:4px;padding:4px 8px;font-size:0.8rem;color:#f85149;border:1px solid #da3633;background:#da363311;border-radius:6px;cursor:pointer;flex-shrink:0" title="Delete">&#x2715;</button>
</div>
{% else %}
<div style="color:#8b949e;font-size:0.85rem;padding:12px">No recordings yet. Waiting for sound events...</div>
{% endfor %}
</div>
<div id="pagination-bottom"></div>

</div>

<script>
// ── Waveform rendering ──────────────────────────────────────────
function drawWaveform(canvas, peaks, progress) {
  const rect = canvas.getBoundingClientRect();
  if (rect.width === 0) { requestAnimationFrame(() => drawWaveform(canvas, peaks, progress)); return; }
  const dpr = window.devicePixelRatio || 2;
  const w = rect.width, h = rect.height;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const mid = h / 2;
  const playedX = (progress || 0) * w;
  const n = peaks.length;

  peaks.forEach((v, i) => {
    const x = (i / n) * w;
    const nextX = ((i + 1) / n) * w;
    const barW = Math.max(0.5, nextX - x - 0.5);
    const barH = Math.max(1, v * (h - 4));
    ctx.fillStyle = x < playedX ? '#388bfd' : '#30363d';
    ctx.fillRect(x, mid - barH / 2, barW, barH);
  });
}

async function loadWaveform(canvas) {
  if (canvas._peaks) return canvas._peaks;
  // Wait for layout so canvas has real dimensions
  if (canvas.getBoundingClientRect().width === 0) {
    await new Promise(r => setTimeout(r, 200));
  }
  const path = canvas.dataset.audiopath.replace('/audio/', '');
  const res = await fetch('/api/waveform/' + path);
  canvas._peaks = await res.json();
  if (canvas._peaks.length) drawWaveform(canvas, canvas._peaks, 0);
  return canvas._peaks;
}

function formatTime(s) {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m + ':' + String(sec).padStart(2,'0');
}

function startPlayheadTick(audio, canvas, peaks, btn, timeEl, playhead) {
  playhead.style.opacity = '1';
  function tick() {
    if (!audio.paused) {
      const progress = audio.currentTime / (audio.duration || 1);
      drawWaveform(canvas, peaks, progress);
      playhead.style.left = (progress * canvas.getBoundingClientRect().width) + 'px';
      timeEl.textContent = formatTime(audio.currentTime) + ' / ' + formatTime(audio.duration || 0);
      requestAnimationFrame(tick);
    }
  }
  requestAnimationFrame(tick);
  audio.onended = () => {
    btn.textContent = '▶';
    playhead.style.opacity = '0';
    drawWaveform(canvas, peaks, 0);
    timeEl.textContent = '0:00 / ' + formatTime(audio.duration || 0);
    // Auto-advance to next newer recording (list is newest-first, so go up)
    const item = audio.closest('.rec-item');
    const nextItem = item.previousElementSibling;
    if (nextItem && nextItem.classList.contains('rec-item')) {
      const nextBtn = nextItem.querySelector('.play-btn');
      if (nextBtn) nextBtn.click();
    }
  };
}

function stopAllPlayers(exceptAudio) {
  document.querySelectorAll('.rec-item').forEach(item => {
    const a = item.querySelector('audio');
    if (a && a !== exceptAudio && !a.paused) {
      a.pause();
      a.currentTime = 0;
      item.querySelector('.play-btn').textContent = '▶';
      item.querySelector('.playhead').style.opacity = '0';
      const cv = item.querySelector('.waveform-canvas');
      if (cv && cv._peaks) drawWaveform(cv, cv._peaks, 0);
      const te = item.querySelector('.time-display');
      if (te) te.textContent = '0:00 / ' + formatTime(a.duration || 0);
    }
  });
}

function togglePlay(btn) {
  const item = btn.closest('.rec-item');
  const audio = item.querySelector('audio');
  const canvas = item.querySelector('.waveform-canvas');
  const playhead = item.querySelector('.playhead');
  const timeEl = item.querySelector('.time-display');

  stopAllPlayers(audio);

  loadWaveform(canvas).then(peaks => {
    if (audio.paused) {
      audio.play();
      btn.textContent = '⏸';
      startPlayheadTick(audio, canvas, peaks, btn, timeEl, playhead);
    } else {
      audio.pause();
      btn.textContent = '▶';
    }
  });
}

function seekAndPlay(el, startSec) {
  const item = el.closest('.rec-item');
  const audio = item.querySelector('audio');
  const canvas = item.querySelector('.waveform-canvas');
  const playhead = item.querySelector('.playhead');
  const timeEl = item.querySelector('.time-display');
  const playBtn = item.querySelector('.play-btn');
  stopAllPlayers(audio);
  loadWaveform(canvas).then(peaks => {
    audio.currentTime = startSec;
    audio.play();
    playBtn.textContent = '⏸';
    startPlayheadTick(audio, canvas, peaks, playBtn, timeEl, playhead);
  });
}

function stopAudio(btn) {
  const item = btn.closest('.rec-item');
  const audio = item.querySelector('audio');
  const canvas = item.querySelector('.waveform-canvas');
  const playhead = item.querySelector('.playhead');
  const timeEl = item.querySelector('.time-display');
  const playBtn = item.querySelector('.play-btn');
  audio.pause();
  audio.currentTime = 0;
  playBtn.textContent = '▶';
  playhead.style.opacity = '0';
  if (canvas._peaks) drawWaveform(canvas, canvas._peaks, 0);
  const dur = audio.duration || 0;
  timeEl.textContent = '0:00 / ' + formatTime(dur);
}

function seekAudio(e) {
  const wrap = e.currentTarget;
  const item = wrap.closest('.rec-item');
  const audio = item.querySelector('audio');
  const canvas = item.querySelector('.waveform-canvas');
  const peaks = canvas._peaks;
  if (!peaks) return;

  const rect = canvas.getBoundingClientRect();
  const seekFraction = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));

  function doSeek() {
    audio.currentTime = seekFraction * audio.duration;
    drawWaveform(canvas, peaks, seekFraction);
    item.querySelector('.playhead').style.left = (seekFraction * canvas.getBoundingClientRect().width) + 'px';
    item.querySelector('.playhead').style.opacity = '1';
    item.querySelector('.time-display').textContent =
      formatTime(audio.currentTime) + ' / ' + formatTime(audio.duration);
  }

  if (audio.paused) {
    // Pause any other playing audio
    document.querySelectorAll('audio').forEach(a => {
      if (a !== audio && !a.paused) {
        a.pause();
        a.closest('.rec-item').querySelector('.play-btn').textContent = '▶';
        a.closest('.rec-item').querySelector('.playhead').style.opacity = '0';
      }
    });
    const btn = item.querySelector('.play-btn');
    if (!audio.duration) {
      audio.preload = 'auto';
      audio.load();
      audio.addEventListener('canplay', () => {
        doSeek();
        audio.play();
        btn.textContent = '⏸';
        startPlayheadTick(audio, canvas, peaks, btn, item.querySelector('.time-display'), item.querySelector('.playhead'));
      }, { once: true });
    } else {
      doSeek();
      audio.play();
      btn.textContent = '⏸';
      startPlayheadTick(audio, canvas, peaks, btn, item.querySelector('.time-display'), item.querySelector('.playhead'));
    }
  } else {
    doSeek();
  }
}

// Lazy-load waveforms as they scroll into view
const observer = new IntersectionObserver(entries => {
  entries.forEach(e => { if (e.isIntersecting) loadWaveform(e.target); });
}, { threshold: 0.1 });
document.querySelectorAll('.waveform-canvas').forEach(c => observer.observe(c));

// ── Filter ─────────────────────────────────────────────────────
const cfg_local_url = '{{ cfg.get("local_url", "") }}';
const cfg_tailscale_url = '{{ cfg.get("tailscale_url", "") }}';
let currentFilter = '{{ tag_filter }}';

function setFilter(f) {
  currentFilter = f;
  document.querySelectorAll('.fbtn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  loadPage(1);
}

async function feedbackTag(e, path, action, label) {
  e.stopPropagation();
  const pill = e.target.closest('.tag');
  const res = await fetch('/api/feedback', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path, action, label})
  });
  const d = await res.json();
  if (action === 'wrong') {
    pill.classList.add('wrong');
    e.target.textContent = '↩';
    e.target.title = 'Restore';
    e.target.setAttribute('onclick', `feedbackTag(event,'${path}','unwrong','${label}')`);
  } else {
    pill.classList.remove('wrong');
    e.target.textContent = '✕';
    e.target.title = 'Mark wrong';
    e.target.setAttribute('onclick', `feedbackTag(event,'${path}','wrong','${label}')`);
  }
}

const ALL_LABELS = {{ label_icons | tojson }};

function showAddTag(btn, path) {
  // Toggle existing select
  const existing = btn.nextElementSibling;
  if (existing && existing.tagName === 'SELECT') { existing.remove(); return; }
  const sel = document.createElement('select');
  sel.style.cssText = 'font-size:0.72rem;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:1px 4px;';
  sel.innerHTML = '<option value="">+ add tag…</option>' +
    Object.entries(ALL_LABELS).map(([l,i]) => `<option value="${l}">${i} ${l}</option>`).join('');
  sel.onchange = async () => {
    const label = sel.value;
    if (!label) return;
    await fetch('/api/feedback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path, action: 'add', label})
    });
    sel.remove();
    // Re-render this item's tags by fetching fresh HTML
    const item = btn.closest('.rec-item');
    const p = path.startsWith('/') ? path.slice(1) : path;
    const res = await fetch('/api/recordings_html?page=1&filter=all');
    const data = await res.json();
    const tmp = document.createElement('div');
    tmp.innerHTML = data.html;
    const fresh = [...tmp.querySelectorAll('.rec-item')].find(el => el.dataset.path === path);
    if (fresh) item.querySelector('.rec-tags').outerHTML = fresh.querySelector('.rec-tags').outerHTML;
  };
  btn.after(sel);
  sel.focus();
}

// ── Pagination ─────────────────────────────────────────────────
let currentPage = {{ page }};
let totalPages  = {{ total_pages }};
let totalRecs   = {{ total }};
const perPage   = {{ per_page }};

function pgBtn(label, page, active, disabled) {
  const base = 'padding:4px 10px;border-radius:5px;cursor:pointer;font-family:inherit;font-size:0.8rem;border:1px solid ';
  const style = active  ? base + '#1f6feb;background:#1f6feb;color:#fff' :
                disabled ? base + '#30363d;background:#0d1117;color:#484f58;cursor:default' :
                           base + '#30363d;background:#21262d;color:#c9d1d9';
  const click = disabled ? '' : `onclick="loadPage(${page})"`;
  return `<button ${click} style="${style}">${label}</button>`;
}

function renderPagination(page, totalPages, total) {
  if (totalPages <= 1) return '';
  const from = (page - 1) * perPage + 1;
  const to   = Math.min(page * perPage, total);
  let html = `<div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin:10px 0">`;
  html += `<span style="font-size:0.75rem;color:#8b949e;margin-right:6px">${from}–${to} of ${total}</span>`;
  html += pgBtn('← Prev', page - 1, false, page <= 1);

  // Smart page numbers: always show 1, last, and window around current
  const show = new Set([1, totalPages]);
  for (let i = Math.max(1, page - 2); i <= Math.min(totalPages, page + 2); i++) show.add(i);
  const sorted = [...show].sort((a, b) => a - b);
  let prev = null;
  for (const p of sorted) {
    if (prev !== null && p > prev + 1) html += `<span style="color:#8b949e;padding:0 2px">…</span>`;
    html += pgBtn(p, p, p === page, false);
    prev = p;
  }

  html += pgBtn('Next →', page + 1, false, page >= totalPages);
  html += `</div>`;
  return html;
}

function updatePaginationUI() {
  const html = renderPagination(currentPage, totalPages, totalRecs);
  document.getElementById('pagination-top').innerHTML = html;
  document.getElementById('pagination-bottom').innerHTML = html;
  document.getElementById('total-count').textContent = totalRecs;
}

async function loadPage(page) {
  if (page < 1 || page > totalPages) return;
  // Stop any playing audio before switching pages
  document.querySelectorAll('audio').forEach(a => { if (!a.paused) a.pause(); });

  const res  = await fetch(`/api/recordings_html?page=${page}&filter=${currentFilter}`);
  const data = await res.json();
  currentPage = data.page;
  totalPages  = data.total_pages;
  totalRecs   = data.total;

  const list = document.querySelector('.recordings-list');
  list.innerHTML = data.html || '<div style="color:#8b949e;font-size:0.85rem;padding:12px">No recordings yet.</div>';

  // Re-register waveform observer for new canvases
  list.querySelectorAll('.waveform-canvas').forEach(c => observer.observe(c));

  // Sync knownPaths so live-insert works correctly on page 1
  if (page === 1) {
    knownPaths.clear();
    list.querySelectorAll('.rec-item').forEach(el => knownPaths.add(el.dataset.path));
  }

  updatePaginationUI();
  history.pushState({page}, '', page === 1 ? location.pathname : `?page=${page}`);
  window.scrollTo({top: document.querySelector('.recordings-list').offsetTop - 60, behavior: 'smooth'});
}

window.addEventListener('popstate', e => { if (e.state?.page) loadPage(e.state.page); });

// Render initial pagination
updatePaginationUI();

// ── Monitor start/stop ─────────────────────────────────────────
async function toggleMonitoring() {
  const btn = document.getElementById('monitor-btn');
  const enable = btn.dataset.enabled !== 'true';
  await fetch('/monitoring', { method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled: enable}) });
}

function updateMonitorBtn(state) {
  const btn = document.getElementById('monitor-btn');
  if (!btn) return;
  const stopped = state === 'stopped';
  btn.dataset.enabled = stopped ? 'false' : 'true';
  btn.textContent = stopped ? '⏺ Start' : '⏹ Stop';
  btn.style.cssText = stopped
    ? 'padding:3px 14px;font-size:0.8rem;border-radius:12px;cursor:pointer;border:1px solid #238636;background:#23863622;color:#3fb950'
    : 'padding:3px 14px;font-size:0.8rem;border-radius:12px;cursor:pointer;border:1px solid #da3633;background:#da363322;color:#f85149';
}

// ── Mode toggle ────────────────────────────────────────────────
async function toggleMode() {
  const btn = document.getElementById('mode-btn');
  const newMode = btn.dataset.mode === 'event' ? 'continuous' : 'event';
  await fetch('/mode', { method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode: newMode}) });
  // pollState will update the button appearance
}

function updateModeBtn(mode) {
  const btn = document.getElementById('mode-btn');
  if (!btn) return;
  btn.dataset.mode = mode;
  if (mode === 'continuous') {
    btn.textContent = '⏺ Continuous';
    btn.style.cssText = 'padding:3px 14px;font-size:0.8rem;border-radius:12px;cursor:pointer;border:1px solid #238636;background:#23863622;color:#3fb950';
  } else {
    btn.textContent = '⚡ Event';
    btn.style.cssText = 'padding:3px 14px;font-size:0.8rem;border-radius:12px;cursor:pointer;border:1px solid #30363d;background:#21262d;color:#c9d1d9';
  }
}

// ── Live listen ────────────────────────────────────────────────
let liveController = null;
let liveAudioCtx = null;
let liveNextTime = 0;

async function toggleLive() {
  const btn = document.getElementById('live-btn');
  if (liveController) {
    liveController.abort();
    liveController = null;
    liveAudioCtx?.close();
    liveAudioCtx = null;
    btn.textContent = '🎤 Live';
    btn.style.color = '';
    btn.style.borderColor = '#30363d';
    return;
  }

  btn.textContent = '⏹ Stop';
  btn.style.color = '#f85149';
  btn.style.borderColor = '#da3633';

  liveAudioCtx = new AudioContext({ sampleRate: 44100 });
  liveNextTime = liveAudioCtx.currentTime + 0.3;
  liveController = new AbortController();

  try {
    const response = await fetch('/live', { signal: liveController.signal });
    const reader = response.body.getReader();
    let buf = new Uint8Array(0);
    const CHUNK = 8820; // 100ms of int16 mono @ 44100Hz

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const tmp = new Uint8Array(buf.length + value.length);
      tmp.set(buf); tmp.set(value, buf.length);
      buf = tmp;

      while (buf.length >= CHUNK) {
        const slice = buf.slice(0, CHUNK);
        buf = buf.slice(CHUNK);
        const pcm = new Int16Array(slice.buffer);
        const ab = liveAudioCtx.createBuffer(1, pcm.length, 44100);
        const ch = ab.getChannelData(0);
        for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768.0;
        const src = liveAudioCtx.createBufferSource();
        src.buffer = ab;
        src.connect(liveAudioCtx.destination);
        const t = Math.max(liveAudioCtx.currentTime + 0.05, liveNextTime);
        src.start(t);
        liveNextTime = t + ab.duration;
      }
    }
  } catch(e) {
    if (e.name !== 'AbortError') console.error('live error:', e);
  }
  if (liveController) {
    liveController = null;
    btn.textContent = '🎤 Live';
    btn.style.color = '';
    btn.style.borderColor = '#30363d';
  }
}

// ── Live recordings ────────────────────────────────────────────
let lastTotalEvents = null;
let lastQueueBusy = null;
const knownPaths = new Set(
  [...document.querySelectorAll('.rec-item')].map(el => el.dataset.path)
);

async function checkNewRecordings() {
  const res  = await fetch(`/api/recordings_html?page=${currentPage}&filter=${currentFilter}`);
  const data = await res.json();
  totalRecs   = data.total;
  totalPages  = data.total_pages;
  updatePaginationUI();
  document.getElementById('total-count').textContent = data.total;

  const tmp = document.createElement('div');
  tmp.innerHTML = data.html;
  const list = document.querySelector('.recordings-list');

  // Never touch items that are currently playing
  const playingPaths = new Set(
    [...document.querySelectorAll('audio')].filter(a => !a.paused).map(a => {
      const item = a.closest('[data-path]');
      return item ? item.dataset.path : null;
    }).filter(Boolean)
  );

  // Insert new items at top, update tags/birds on existing non-playing items
  tmp.querySelectorAll('.rec-item').forEach(newEl => {
    const path = newEl.dataset.path;
    const existing = list.querySelector(`[data-path="${CSS.escape(path)}"]`);
    if (!existing) {
      // Genuinely new — prepend
      knownPaths.add(path);
      const placeholder = [...list.children].find(el => !el.classList.contains('rec-item'));
      if (placeholder) placeholder.remove();
      list.prepend(newEl);
      const canvas = newEl.querySelector('.waveform-canvas');
      if (canvas) observer.observe(canvas);
    } else if (!playingPaths.has(path)) {
      // Existing, not playing — update tags/birds/analyzing indicator only
      const newTags = newEl.querySelector('.rec-tags');
      const oldTags = existing.querySelector('.rec-tags');
      if (newTags && oldTags) oldTags.innerHTML = newTags.innerHTML;
      const newBirds = newEl.querySelector('.rec-birds');
      const oldBirds = existing.querySelector('.rec-birds');
      if (newBirds && oldBirds) oldBirds.outerHTML = newBirds.outerHTML;
      else if (newBirds && !oldBirds) {
        const tagsEl = existing.querySelector('.rec-tags');
        if (tagsEl) tagsEl.insertAdjacentHTML('afterend', newBirds.outerHTML);
      }
      const newImgs = newEl.querySelector('.bird-img-wrap');
      const oldImgs = existing.querySelector('.bird-img-wrap');
      if (newImgs && oldImgs) oldImgs.outerHTML = newImgs.outerHTML;
      else if (newImgs && !oldImgs) {
        const birdsEl = existing.querySelector('.rec-birds');
        if (birdsEl) birdsEl.insertAdjacentHTML('afterend', newImgs.outerHTML);
      }
    }
  });
}

// ── Analysis result polling ────────────────────────────────────
async function pollAnalysis() {
  for (const item of document.querySelectorAll('.rec-item')) {
    const tagsEl = item.querySelector('.rec-tags');
    // Only poll items still awaiting results
    if (tagsEl && !tagsEl.querySelector('.tag')) {
      const p = item.dataset.path.startsWith('/') ? item.dataset.path.slice(1) : item.dataset.path;
      try {
        const d = await (await fetch('/api/analysis/' + p.replace('.wav','') + '.wav')).json();
        if (d.status === 'done') {
          // Rebuild tags
          let html = '';
          if (d.tags && d.tags.length) {
            html += d.tags.map(t =>
              `<span class="tag">${t.icon} ${t.label} <span class="tag-conf">${Math.round(t.confidence*100)}%</span></span>`
            ).join('');
          }
          tagsEl.innerHTML = html || '';
          // Show transcript
          if (d.transcript && d.transcript.text) {
            let tx = item.querySelector('.rec-transcript');
            if (!tx) {
              tx = document.createElement('div');
              tx.className = 'rec-transcript';
              tagsEl.after(tx);
            }
            tx.textContent = d.transcript.text;
          }
        }
      } catch(e) {}
    }
  }
  setTimeout(pollAnalysis, 5000);
}
pollAnalysis();

// ── Disk usage ─────────────────────────────────────────────────
function fmtBytes(b) {
  if (b >= 1e12) return (b/1e12).toFixed(2) + ' TB';
  if (b >= 1e9)  return (b/1e9).toFixed(2) + ' GB';
  if (b >= 1e6)  return (b/1e6).toFixed(1) + ' MB';
  return (b/1e3).toFixed(0) + ' KB';
}

async function pollDisk() {
  try {
    const d = await (await fetch('/api/disk')).json();
    document.getElementById('disk-rec-size').textContent = fmtBytes(d.recordings_bytes);
    const freeOf = fmtBytes(d.device_free) + ' free / ' + fmtBytes(d.device_total);
    document.getElementById('disk-free').textContent = freeOf;
    document.getElementById('disk-mount').textContent = d.mount;
    const maxGb = parseFloat(document.querySelector('[name=max_recordings_gb]')?.value || 10);
    const pct = Math.min(100, d.recordings_bytes / (maxGb * 1e9) * 100);
    document.getElementById('disk-bar').style.width = pct + '%';
    document.getElementById('disk-bar-mini').style.width = pct + '%';
    const pctFree = d.device_total > 0 ? Math.round((d.device_free / d.device_total) * 100) : 0;
    document.getElementById('disk-summary').textContent =
      fmtBytes(d.recordings_bytes) + ' used · ' + pctFree + '% device free';
    document.getElementById('disk-sd-warn').style.display = d.on_sd ? '' : 'none';
    // update arrow on details toggle
    const det = document.getElementById('storage-details');
    if (det) {
      const arrow = det.querySelector('summary span:last-child');
      if (arrow) arrow.textContent = det.open ? '▼' : '▶';
      det.addEventListener('toggle', () => { if(arrow) arrow.textContent = det.open ? '▼' : '▶'; }, {once:false});
    }
  } catch(e) {}
  setTimeout(pollDisk, 10000);
}
pollDisk();

async function pollState() {
  try {
    const r = await fetch('/api/state');
    const s = await r.json();
    const threshold = s.threshold || 0.001;
    const pct = Math.min(s.rms / (threshold * 2) * 100, 100);
    document.getElementById('meter-fill').style.width = pct + '%';
    document.getElementById('rms-val').textContent = s.rms.toFixed(4);
    document.getElementById('thr-val').textContent = s.threshold.toFixed(4);
    document.getElementById('baseline-val').textContent = s.baseline_rms.toFixed(4);
    document.getElementById('mult-val').textContent = s.threshold_multiplier + '×';
    const badge = document.getElementById('status-badge');
    badge.textContent = s.state;
    badge.className = 'status ' + (s.state === 'recording' ? 'recording' : 'idle');
    document.getElementById('events-count').textContent = s.total_events + ' events recorded';
    if (lastTotalEvents !== null && s.total_events > lastTotalEvents) checkNewRecordings();
    lastTotalEvents = s.total_events;
    if (s.mode) updateModeBtn(s.mode);
    updateMonitorBtn(s.state);
    // CPU indicator
    const cpuEl = document.getElementById('cpu-pct');
    if (cpuEl && s.cpu_pct != null) {
      const temp = s.cpu_temp ? ` ${s.cpu_temp}°` : '';
      cpuEl.textContent = s.cpu_pct + '%' + temp;
      const cpuInd = document.getElementById('cpu-indicator');
      cpuInd.style.color = s.cpu_pct > 85 ? '#f85149' : s.cpu_pct > 60 ? '#d29922' : '#8b949e';
      cpuInd.style.borderColor = s.cpu_pct > 85 ? '#da3633' : s.cpu_pct > 60 ? '#d2992255' : '#30363d';
    }
    const calAlert = document.getElementById('cal-alert');
    if (s.calibrating && s.cal_started) {
      const remaining = Math.max(0, Math.ceil(10 - (Date.now() / 1000 - s.cal_started)));
      document.getElementById('cal-countdown').textContent = remaining;
      calAlert.style.display = '';
    } else {
      calAlert.style.display = 'none';
    }
  } catch(e) {}
  // Poll analysis queue separately
  try {
    const qr = await fetch('/api/queue');
    const q = await qr.json();
    const ind = document.getElementById('queue-indicator');
    const lbl = document.getElementById('queue-label');
    if (q.current || q.pending_count > 0) {
      lbl.textContent = q.current
        ? 'analyzing' + (q.pending_count > 0 ? ` (+${q.pending_count} queued)` : '') + '…'
        : `${q.pending_count} queued…`;
      ind.style.display = '';
      lastQueueBusy = true;
    } else {
      if (lastQueueBusy === true) { checkNewRecordings(); }
      ind.style.display = 'none';
      lastQueueBusy = false;
    }
  } catch(e) {}
  setTimeout(pollState, 1000);
}
pollState();

function _copyAndToast(url, label) {
  function _toast(text, color) {
    const msg = document.createElement('div');
    msg.textContent = text;
    msg.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:'+color+';color:white;padding:8px 18px;border-radius:8px;font-size:0.85rem;z-index:9999';
    document.body.appendChild(msg);
    setTimeout(() => msg.remove(), 3000);
  }
  // Try clipboard API first, then execCommand fallback, then prompt
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(url).then(() => _toast(label + ' link copied!', '#238636'));
  } else {
    const ta = document.createElement('textarea');
    ta.value = url;
    ta.style.cssText = 'position:fixed;left:-9999px';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      _toast(label + ' link copied!', '#238636');
    } catch(e) {
      _toast('Copy this link:', '#d29922');
      prompt('Copy link:', url);
    }
    ta.remove();
  }
}
function shareLAN(name) {
  const base = cfg_local_url || window.location.origin;
  _copyAndToast(base + '/birds/clip/' + encodeURIComponent(name), 'LAN');
}
function shareTS(name) {
  const base = cfg_tailscale_url || window.location.origin;
  _copyAndToast(base + '/clip/' + encodeURIComponent(name), 'Tailscale');
}

async function confirmBird(evt, path, label, correct) {
  evt.stopPropagation();
  const btn = evt.target;
  btn.disabled = true;
  await fetch('/api/confirm_bird', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path, label, correct})
  });
  // Visual feedback
  const det = btn.closest('.bird-det') || btn.previousElementSibling;
  if (correct) {
    btn.style.background = '#238636';
    btn.style.color = 'white';
  } else {
    btn.parentElement.style.opacity = '0.4';
    btn.parentElement.style.textDecoration = 'line-through';
  }
}

async function deleteRec(btn, path) {
  const item = btn.closest('[data-path]');
  if (!item) return;
  const fd = new FormData();
  fd.append('path', path);
  btn.disabled = true;
  btn.textContent = '…';
  const r = await fetch('/delete', {method:'POST', body: fd});
  if (r.ok) {
    item.parentElement.remove ? item.remove() : item.parentElement.removeChild(item);
    // Update total count display if present
    const tc = document.getElementById('total-count');
    if (tc) tc.textContent = parseInt(tc.textContent||0) - 1;
  } else {
    btn.disabled = false;
    btn.textContent = '✕';
  }
}

</script>
</body>
</html>
"""


PER_PAGE = 20


def paginate(recs, page, per_page=PER_PAGE):
    total = len(recs)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    return recs[start:start + per_page], total, total_pages, page


@app.route("/")
@require_auth
def index():
    page = int(request.args.get("page", 1))
    tag_filter = request.args.get("filter", "all")
    all_recs = get_recordings(tag_filter)
    recs, total, total_pages, page = paginate(all_recs, page)
    bird_counts, corvid_total = get_today_bird_counts()
    return render_template_string(TEMPLATE,
                                  state=load_state(),
                                  cfg=load_config(),
                                  recordings=recs,
                                  page=page,
                                  total_pages=total_pages,
                                  total=total,
                                  per_page=PER_PAGE,
                                  tag_filter=tag_filter,
                                  known_labels=KNOWN_LABELS,
                                  label_icons=LABEL_ICONS,
                                  bird_counts=bird_counts,
                                  corvid_total=corvid_total,
                                  detected_species=get_detected_species()[0],
                                  detected_sounds=get_detected_species()[1])


@app.route("/api/state")
@require_auth
def api_state():
    state = load_state()
    # CPU% from background cache (updated every 2s, no blocking)
    state["cpu_pct"] = _cpu_cache.get("pct")
    state["cpu_temp"] = _cpu_cache.get("temp")
    return jsonify(state)


@app.route("/api/recordings")
@require_auth
def api_recordings():
    return jsonify(get_recordings())


RECORDING_ITEM_TEMPLATE = """
<div class="rec-item" data-path="{{ rec.path }}">
  <div class="rec-player">
    <div style="display:flex;justify-content:space-between;align-items:baseline">
      <div class="rec-name">{{ rec.display_time }}</div>
      <div class="rec-meta">{{ rec.duration }} &nbsp;{{ rec.size_kb }} KB</div>
    </div>
    <div class="waveform-wrap" onclick="seekAudio(event)">
      <canvas class="waveform-canvas" data-audiopath="/audio/{{ rec.path | urlencode }}"></canvas>
      <div class="playhead"></div>
    </div>
    <div class="player-controls">
      <button class="play-btn" onclick="togglePlay(this)">&#9654;</button>
      <button class="stop-btn" onclick="stopAudio(this)" title="Stop">&#9632;</button>
      <span class="time-display">0:00 / {{ rec.duration if rec.duration is defined else '&mdash;' }}</span>
    </div>
    <div class="rec-tags">
      {% for tag in rec.tags %}
      <span class="tag {% if tag.label in rec.wrong_tags %}wrong{% endif %}{% if tag.manual is defined and tag.manual %} manual{% endif %}">
        {{ tag.icon }} {{ tag.label }}{% if not tag.manual is defined or not tag.manual %} <span class="tag-conf">{{ (tag.confidence * 100)|int }}%</span>{% endif %}
        {% if tag.label in rec.wrong_tags %}
        <button class="tag-x" onclick="feedbackTag(event,'{{ rec.path }}','unwrong','{{ tag.label }}')" title="Restore">↩</button>
        {% else %}
        <button class="tag-x" onclick="feedbackTag(event,'{{ rec.path }}','wrong','{{ tag.label }}')" title="Mark wrong">✕</button>
        {% endif %}
      </span>
      {% endfor %}
      {% if rec.analysis_status %}
      <button class="tag-add" onclick="showAddTag(this,'{{ rec.path }}')">+ tag</button>
      {% endif %}
      {% if rec.analysis_status == 'processing' %}
      <span style="color:#f0883e;font-size:0.75rem">⏳ analyzing…</span>
      {% endif %}
    </div>
    {% if rec.birds %}
    <div class="rec-birds">
      {% for b in rec.birds %}
      {% if b.start is defined %}<span class="bird-det" style="cursor:pointer" onclick="seekAndPlay(this, {{ b.start }})" title="Jump to {{ b.start }}s">{{ b.icon }} <strong>{{ b.label }}</strong> <span class="tag-conf">{{ (b.confidence*100)|int }}%</span> <span style="color:#8b949e;font-size:0.7rem">▶ {{ b.start }}–{{ b.end }}s</span></span>{% else %}<span class="bird-det">{{ b.icon }} <strong>{{ b.label }}</strong> <span class="tag-conf">{{ (b.confidence*100)|int }}%</span></span>{% endif %}{% if b.crow_name is defined %} <span style="font-size:0.72rem;background:#1a1a2e;border:1px solid #6f42c1;border-radius:8px;padding:1px 7px;color:#d2a8ff">{{ b.crow_name }}{% if b.is_new_crow %} ✨{% endif %} <span style="color:#8b949e">#{{ b.crow_sightings }}</span></span>{% endif %} <button onclick="confirmBird(event,'{{ rec.path | e }}','{{ b.label | e }}',true)" style="background:#23863622;border:1px solid #238636;border-radius:4px;color:#3fb950;font-size:0.7rem;padding:1px 5px;cursor:pointer" title="Confirm">✓</button><button onclick="confirmBird(event,'{{ rec.path | e }}','{{ b.label | e }}',false)" style="background:#da363311;border:1px solid #da3633;border-radius:4px;color:#f85149;font-size:0.7rem;padding:1px 5px;cursor:pointer;margin-left:2px" title="Wrong">✗</button>
      {% endfor %}
    </div>
    {% set birds_with_images = rec.birds | selectattr('image_url', 'defined') | list %}
    {% if birds_with_images %}
    <div class="bird-img-wrap">
      {% for b in birds_with_images %}
      <div class="bird-img-card" onclick="this.classList.toggle('expanded')" title="{{ b.label }}">
        <img src="{{ b.image_url }}" alt="{{ b.label }}" loading="lazy">
        <div class="bird-img-label">{{ b.icon }} {{ b.label }} {{ (b.confidence*100)|int }}%</div>
      </div>
      {% endfor %}
    </div>
    {% endif %}
    {% endif %}
    {% if rec.transcript and rec.transcript.text %}
    <div class="rec-transcript">{{ rec.transcript.text }}</div>
    {% endif %}
    {% if rec.video %}
    <div class="rec-photo">
      <video src="/api/camera/photo/{{ rec.video }}" controls preload="none" poster="{% if rec.photo %}/api/camera/photo/{{ rec.photo }}{% endif %}"
             style="width:100%;max-width:400px;border-radius:6px;border:1px solid #30363d"></video>
    </div>
    {% elif rec.photo %}
    <div class="rec-photo">
      <img src="/api/camera/photo/{{ rec.photo }}" alt="captured" loading="lazy"
           onclick="this.classList.toggle('expanded')" title="Click to expand">
    </div>
    {% endif %}
    <audio src="/audio/{{ rec.path | urlencode }}" preload="none"></audio>
  </div>
  <button onclick="shareLAN('{{ rec.name | e }}')" style="margin-left:4px;padding:4px 6px;font-size:0.7rem;color:#3fb950;border:1px solid #238636;background:#23863611;border-radius:6px;cursor:pointer;flex-shrink:0" title="Copy LAN link (friends)">&#x1F517; LAN</button>
  <button onclick="shareTS('{{ rec.name | e }}')" style="margin-left:2px;padding:4px 6px;font-size:0.7rem;color:#58a6ff;border:1px solid #1f6feb;background:#1f6feb11;border-radius:6px;cursor:pointer;flex-shrink:0" title="Copy Tailscale link">&#x1F517; TS</button>
  <button onclick="deleteRec(this, '{{ rec.path | e }}')" style="margin-left:4px;padding:4px 8px;font-size:0.8rem;color:#f85149;border:1px solid #da3633;background:#da363311;border-radius:6px;cursor:pointer;flex-shrink:0" title="Delete">&#x2715;</button>
</div>
"""


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    data = request.json
    p = Path(data["path"])
    sidecar = p.with_suffix(".json")
    result = json.loads(sidecar.read_text()) if sidecar.exists() else {"status": "done", "tags": []}
    fb = result.setdefault("feedback", {"wrong": [], "manual": []})

    action = data.get("action")
    label  = data.get("label", "")

    if action == "wrong":
        if label not in fb["wrong"]:
            fb["wrong"].append(label)
    elif action == "unwrong":
        fb["wrong"] = [l for l in fb["wrong"] if l != label]
    elif action == "add":
        if label not in fb.setdefault("manual", []):
            fb["manual"].append(label)
        tags = result.setdefault("tags", [])
        if not any(t["label"] == label for t in tags):
            tags.append({"label": label, "icon": LABEL_ICONS.get(label, "🔊"),
                         "confidence": 1.0, "manual": True})
    elif action == "remove":
        fb["manual"] = [l for l in fb.get("manual", []) if l != label]
        result["tags"] = [t for t in result.get("tags", [])
                          if not (t.get("manual") and t["label"] == label)]

    sidecar.write_text(json.dumps(result, indent=2))
    return jsonify({"ok": True, "wrong": fb.get("wrong", [])})


@app.route("/api/analysis/<path:filepath>")
def api_analysis(filepath):
    sidecar = Path("/" + filepath).with_suffix(".json")
    if not sidecar.exists():
        return jsonify({"status": "pending"})
    try:
        return jsonify(json.loads(sidecar.read_text()))
    except Exception:
        return jsonify({"status": "error"})







# ── Public bird feed (no auth required) ─────────────────────────

BIRD_FEED_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mimir — bird feed</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', monospace; background: #0d1117; color: #c9d1d9; min-height: 100vh; }
  .container { max-width: 700px; margin: 0 auto; padding: 20px; }
  h1 { color: #58a6ff; font-size: 1.1rem; margin-bottom: 4px; }
  .subtitle { color: #8b949e; font-size: 0.8rem; margin-bottom: 20px; }
  .bird-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; margin-bottom: 10px; }
  .bird-card .time { color: #e6edf3; font-weight: 600; font-size: 0.9rem; }
  .bird-card .duration { color: #8b949e; font-size: 0.8rem; margin-left: 8px; }
  .bird-det { font-size: 0.82rem; background: #0d1f0d; border: 1px solid #238636; border-radius: 8px; padding: 3px 10px; color: #3fb950; display: inline-block; margin: 4px 2px; cursor: pointer; }
  .bird-det:hover { background: #0d2f0d; }
  .bird-img { max-width: 140px; border-radius: 6px; border: 1px solid #30363d; margin: 6px 4px; cursor: zoom-in; transition: max-width 0.2s; }
  .bird-img.expanded { max-width: 100%; cursor: zoom-out; }
  .waveform-wrap { position: relative; cursor: pointer; margin: 8px 0; }
  .waveform-canvas { width: 100%; height: 52px; display: block; border-radius: 4px; background: #0a0e14; }
  .playhead { position: absolute; top: 0; bottom: 0; width: 2px; background: #58a6ff; pointer-events: none; opacity: 0; }
  .player-controls { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .play-btn { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; width: 30px; height: 30px; border-radius: 50%; cursor: pointer; font-size: 0.75rem; display: flex; align-items: center; justify-content: center; }
  .play-btn:hover { background: #30363d; }
  .time-display { font-size: 0.75rem; color: #8b949e; }
  .none { color: #8b949e; font-size: 0.85rem; padding: 24px 0; }
</style>
</head>
<body>
<div class="container">
  <h1>mimir bird feed</h1>
  <div class="subtitle">Real-time bird detections from Capitol Hill, Seattle <span style="float:right"><a href="/stats" style="color:#58a6ff;text-decoration:none;font-size:0.8rem">Stats</a> &middot; <a href="/login" style="color:#8b949e;text-decoration:none;font-size:0.8rem">Login</a></span></div>
  {% if bird_counts %}
  <div id="counts-bar" style="margin-bottom:16px;display:flex;flex-wrap:wrap;gap:4px">
    {% for label, count in bird_counts|dictsort(by='value', reverse=true) %}
    <span style="font-size:0.75rem;background:#0d1f0d;border:1px solid #238636;border-radius:10px;padding:1px 8px;color:#3fb950">{{ label }} <strong>{{ count }}</strong></span>
    {% endfor %}
  </div>
  {% endif %}
  {% for rec in recordings %}
  <div class="bird-card" data-path="{{ rec.path }}">
    <span class="time">{{ rec.display_time }}</span>
    <span class="duration">{{ rec.duration }}</span>
    <div style="margin-top:6px">
      {% for b in rec.birds %}
      <span class="bird-det" onclick="seekTo(this, {{ b.start if b.start is defined else 0 }})">
        {{ b.icon }} <strong>{{ b.label }}</strong> {{ (b.confidence*100)|int }}%
        {% if b.start is defined %}<span style="color:#8b949e;font-size:0.7rem">&#9654; {{ b.start }}s</span>{% endif %}
      </span>
      {% endfor %}
    </div>
    <div class="waveform-wrap" onclick="seekWaveform(event)">
      <canvas class="waveform-canvas" data-audiopath="/audio/{{ rec.path | urlencode }}"></canvas>
      <div class="playhead"></div>
    </div>
    <div class="player-controls">
      <button class="play-btn" onclick="togglePlay(this)">&#9654;</button>
      <span class="time-display">0:00 / {{ rec.duration }}</span>
    </div>
    <audio src="/audio/{{ rec.path | urlencode }}" preload="none"></audio>
    {% for b in rec.birds %}{% if b.image_url is defined %}
    <div style="display:inline-block;text-align:center;margin:4px;vertical-align:top">
      <img class="bird-img" src="{{ b.image_url }}" alt="{{ b.label }}" onclick="this.classList.toggle('expanded')">
      <div style="font-size:0.72rem;color:#3fb950;margin-top:2px">{{ b.icon }} {{ b.label }}</div>
    </div>
    {% endif %}{% endfor %}
  </div>
  {% else %}
  <div class="none">No bird detections yet today. Check back later!</div>
  {% endfor %}
</div>
<script>
function formatTime(s){if(!s||isNaN(s))return'0:00';return Math.floor(s/60)+':'+('0'+Math.floor(s%60)).slice(-2)}
async function loadWaveform(canvas){
  if(canvas._peaks)return canvas._peaks;
  try{const p=canvas.dataset.audiopath.replace('/audio/','');
  const r=await fetch('/api/waveform/'+p);canvas._peaks=await r.json();drawWaveform(canvas,canvas._peaks,0)}
  catch(e){canvas._peaks=[]}return canvas._peaks;
}
function drawWaveform(canvas,peaks,progress){
  if(!peaks||!peaks.length)return;
  const rect=canvas.getBoundingClientRect();
  if(rect.width===0){requestAnimationFrame(()=>drawWaveform(canvas,peaks,progress));return}
  const dpr=window.devicePixelRatio||2;
  const ctx=canvas.getContext('2d');const w=canvas.width=Math.round(rect.width*dpr);const h=canvas.height=Math.round(rect.height*dpr);
  ctx.clearRect(0,0,w,h);const barW=Math.max(1,w/peaks.length);const mid=h/2;
  for(let i=0;i<peaks.length;i++){const amp=peaks[i]*mid*0.9;ctx.fillStyle=(i/peaks.length<progress)?'#58a6ff':'#30363d';ctx.fillRect(i*barW,mid-amp,barW-1,amp*2||1)}
}
function stopAllAudio(exceptAudio){
  document.querySelectorAll('.bird-card').forEach(c=>{
    const a=c.querySelector('audio');
    if(a&&a!==exceptAudio&&!a.paused){
      a.pause();a.currentTime=0;
      c.querySelector('.play-btn').textContent='\u25B6';
      c.querySelector('.playhead').style.opacity='0';
      const cv=c.querySelector('.waveform-canvas');
      if(cv&&cv._peaks)drawWaveform(cv,cv._peaks,0);
      c.querySelector('.time-display').textContent='0:00 / '+formatTime(a.duration||0);
    }
  });
}
function startTick(audio,canvas,peaks,btn,ph,te){
  (function tick(){if(audio.paused)return;const p=audio.currentTime/(audio.duration||1);drawWaveform(canvas,peaks,p);ph.style.left=(p*100)+'%';ph.style.opacity='1';te.textContent=formatTime(audio.currentTime)+' / '+formatTime(audio.duration);requestAnimationFrame(tick)})();
}
function togglePlay(btn){
  const card=btn.closest('.bird-card');const audio=card.querySelector('audio');const canvas=card.querySelector('.waveform-canvas');
  const ph=card.querySelector('.playhead');const te=card.querySelector('.time-display');
  stopAllAudio(audio);
  loadWaveform(canvas).then(peaks=>{
    if(audio.paused){audio.play();btn.textContent='\u23F8';startTick(audio,canvas,peaks,btn,ph,te)}
    else{audio.pause();btn.textContent='\u25B6'}
  });
}
function seekTo(el,sec){
  const card=el.closest('.bird-card');const audio=card.querySelector('audio');const canvas=card.querySelector('.waveform-canvas');
  const btn=card.querySelector('.play-btn');const ph=card.querySelector('.playhead');const te=card.querySelector('.time-display');
  stopAllAudio(audio);
  loadWaveform(canvas).then(peaks=>{audio.currentTime=sec;audio.play();btn.textContent='\u23F8';startTick(audio,canvas,peaks,btn,ph,te)});
}
function seekWaveform(e){
  const canvas=e.target.closest('.waveform-wrap').querySelector('canvas');if(!canvas)return;
  const rect=canvas.getBoundingClientRect();const frac=(e.clientX-rect.left)/rect.width;
  const card=canvas.closest('.bird-card');const audio=card.querySelector('audio');
  loadWaveform(canvas).then(peaks=>{audio.currentTime=frac*(audio.duration||0);drawWaveform(canvas,peaks,frac)});
}
// Auto-load visible waveforms
const observer=new IntersectionObserver(entries=>{entries.forEach(e=>{if(e.isIntersecting)loadWaveform(e.target)})},{threshold:0.1});
document.querySelectorAll('.waveform-canvas').forEach(c=>observer.observe(c));

// Auto-refresh: poll for new bird detections every 5s
const knownPaths = new Set([...document.querySelectorAll('.bird-card')].map(el => el.dataset.path));
async function pollBirds() {
  try {
    const r = await fetch('/api/birds_feed');
    const d = await r.json();
    if (!d.html) { setTimeout(pollBirds, 5000); return; }
    const tmp = document.createElement('div');
    tmp.innerHTML = d.html;
    const container = document.querySelector('.container');
    const firstCard = container.querySelector('.bird-card');
    const none = container.querySelector('.none');
    let added = false;
    tmp.querySelectorAll('.bird-card').forEach(el => {
      const path = el.dataset.path;
      if (!knownPaths.has(path)) {
        knownPaths.add(path);
        if (none) none.remove();
        if (firstCard) firstCard.before(el);
        else container.appendChild(el);
        const canvas = el.querySelector('.waveform-canvas');
        if (canvas) observer.observe(canvas);
        added = true;
      }
    });
    // Update counts bar
    if (added && d.counts_html) {
      let bar = document.getElementById('counts-bar');
      if (bar) { bar.innerHTML = d.counts_html; }
      else {
        const sub = container.querySelector('.subtitle');
        if (sub) { sub.insertAdjacentHTML('afterend', '<div id="counts-bar" style="margin-bottom:16px;display:flex;flex-wrap:wrap;gap:4px">' + d.counts_html + '</div>'); }
      }
    }
  } catch(e) {}
  setTimeout(pollBirds, 5000);
}
pollBirds();
</script>
</body>
</html>
"""


@app.route("/birds")
def public_bird_feed():
    from datetime import datetime
    from collections import defaultdict
    cfg = load_config()
    rdir = Path(cfg["recordings_dir"])
    BLOCKED_TAGS = {"speech"}
    import wave as wavelib

    bird_recs = []
    bird_counts = defaultdict(int)
    today = datetime.now().strftime("%Y-%m-%d")

    for sidecar in sorted(rdir.rglob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            d = json.loads(sidecar.read_text())
            if d.get("status") != "done": continue
            birds = d.get("birds", [])
            if not birds: continue
            tags = {t["label"] for t in d.get("tags", [])}
            if tags & BLOCKED_TAGS: continue  # skip clips with speech
            wav = sidecar.with_suffix(".wav")
            if not wav.exists(): continue
            mt = datetime.fromtimestamp(wav.stat().st_mtime)
            now = datetime.now()
            if mt.date() == now.date():
                day = "Today"
            elif (now.date() - mt.date()).days == 1:
                day = "Yesterday"
            else:
                day = mt.strftime("%a %b %-d")
            display_time = f"{day} {mt.strftime('%-I:%M %p')}"
            try:
                with wavelib.open(str(wav), "r") as wf:
                    secs = wf.getnframes() / wf.getframerate()
                    duration = f"{int(secs//60)}:{int(secs%60):02d}"
            except Exception:
                duration = "\u2014"
            bird_recs.append({
                "path": str(wav).lstrip("/"), "display_time": display_time,
                "duration": duration, "birds": birds,
            })
            if mt.strftime("%Y-%m-%d") == today:
                for b in birds:
                    bird_counts[b["label"]] += 1
        except Exception:
            continue
        if len(bird_recs) >= 50:
            break

    return render_template_string(BIRD_FEED_TEMPLATE,
        recordings=bird_recs, bird_counts=dict(bird_counts))


BIRD_CARD_TEMPLATE = """{% for rec in recs %}<div class="bird-card" data-path="{{ rec.path }}">
<span class="time">{{ rec.display_time }}</span><span class="duration">{{ rec.duration }}</span>
<div style="margin-top:6px">{% for b in rec.birds %}<span class="bird-det" onclick="seekTo(this, {{ b.start if b.start is defined else 0 }})">{{ b.icon }} <strong>{{ b.label }}</strong> {{ (b.confidence*100)|int }}%{% if b.start is defined %} <span style="color:#8b949e;font-size:0.7rem">&#9654; {{ b.start }}s</span>{% endif %}</span>{% endfor %}</div>
<div class="waveform-wrap" onclick="seekWaveform(event)"><canvas class="waveform-canvas" data-audiopath="/audio/{{ rec.path | urlencode }}"></canvas><div class="playhead"></div></div>
<div class="player-controls"><button class="play-btn" onclick="togglePlay(this)">&#9654;</button><span class="time-display">0:00 / {{ rec.duration }}</span></div>
<audio src="/audio/{{ rec.path | urlencode }}" preload="none"></audio>
{% for b in rec.birds %}{% if b.image_url is defined %}<div style="display:inline-block;text-align:center;margin:4px;vertical-align:top"><img class="bird-img" src="{{ b.image_url }}" alt="{{ b.label }}" onclick="this.classList.toggle('expanded')"><div style="font-size:0.72rem;color:#3fb950;margin-top:2px">{{ b.icon }} {{ b.label }}</div></div>{% endif %}{% endfor %}
</div>{% endfor %}"""


@app.route("/api/birds_feed")
def api_birds_feed():
    from datetime import datetime
    from collections import defaultdict
    import wave as wavelib
    cfg = load_config()
    rdir = Path(cfg["recordings_dir"])
    BLOCKED_TAGS = {"speech"}
    today = datetime.now().strftime("%Y-%m-%d")
    bird_recs = []
    bird_counts = defaultdict(int)
    for sidecar in sorted(rdir.rglob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            d = json.loads(sidecar.read_text())
            if d.get("status") != "done": continue
            birds = d.get("birds", [])
            if not birds: continue
            tags = {t["label"] for t in d.get("tags", [])}
            if tags & BLOCKED_TAGS: continue
            wav = sidecar.with_suffix(".wav")
            if not wav.exists(): continue
            mt = datetime.fromtimestamp(wav.stat().st_mtime)
            now = datetime.now()
            if mt.date() == now.date():
                day = "Today"
            elif (now.date() - mt.date()).days == 1:
                day = "Yesterday"
            else:
                day = mt.strftime("%a %b %-d")
            display_time = f"{day} {mt.strftime('%-I:%M %p')}"
            try:
                with wavelib.open(str(wav), "r") as wf:
                    secs = wf.getnframes() / wf.getframerate()
                    duration = f"{int(secs//60)}:{int(secs%60):02d}"
            except Exception:
                duration = "\u2014"
            bird_recs.append({"path": str(wav).lstrip("/"), "display_time": display_time,
                              "duration": duration, "birds": birds})
            if mt.strftime("%Y-%m-%d") == today:
                for b in birds:
                    bird_counts[b["label"]] += 1
        except Exception:
            continue
        if len(bird_recs) >= 50:
            break
    html = render_template_string(BIRD_CARD_TEMPLATE, recs=bird_recs)
    counts_html = "".join(
        f'<span style="font-size:0.75rem;background:#0d1f0d;border:1px solid #238636;border-radius:10px;padding:1px 8px;color:#3fb950">{k} <strong>{v}</strong></span>'
        for k, v in sorted(bird_counts.items(), key=lambda x: -x[1]))
    return jsonify({"html": html, "count": len(bird_recs), "counts_html": counts_html})

@app.route("/api/birds_count")
def api_birds_count():
    cfg = load_config()
    rdir = Path(cfg["recordings_dir"])
    BLOCKED_TAGS = {"speech"}
    count = 0
    for sidecar in rdir.rglob("*.json"):
        try:
            d = json.loads(sidecar.read_text())
            if d.get("status") != "done": continue
            if not d.get("birds"): continue
            tags = {t["label"] for t in d.get("tags", [])}
            if tags & BLOCKED_TAGS: continue
            count += 1
        except: pass
    return jsonify({"count": count})


CLIP_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mimir clip</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', monospace; background: #0d1117; color: #c9d1d9; min-height: 100vh; }
  .container { max-width: 640px; margin: 0 auto; padding: 20px; }
  a.back { color: #8b949e; font-size: 0.85rem; text-decoration: none; }
  a.back:hover { color: #58a6ff; }
  h2 { color: #e6edf3; font-size: 1.1rem; font-weight: 600; margin: 12px 0 4px; }
  .meta { color: #8b949e; font-size: 0.8rem; margin-bottom: 16px; }
  .waveform-wrap { position: relative; cursor: pointer; margin: 8px 0; }
  .waveform-canvas { width: 100%; height: 64px; display: block; border-radius: 6px; background: #0a0e14; }
  .playhead { position: absolute; top: 0; bottom: 0; width: 2px; background: #58a6ff; pointer-events: none; opacity: 0; }
  .player-controls { display: flex; align-items: center; gap: 10px; margin: 8px 0 16px; }
  .play-btn, .stop-btn { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; width: 36px; height: 36px; border-radius: 50%; cursor: pointer; font-size: 0.85rem; display: flex; align-items: center; justify-content: center; }
  .play-btn:hover, .stop-btn:hover { background: #30363d; }
  .time-display { font-size: 0.8rem; color: #8b949e; }
  .tag { font-size: 0.78rem; background: #21262d; border: 1px solid #30363d; border-radius: 10px; padding: 2px 8px; color: #c9d1d9; display: inline-block; margin: 2px; }
  .tag-conf { color: #8b949e; }
  .bird-det { font-size: 0.82rem; background: #0d1f0d; border: 1px solid #238636; border-radius: 8px; padding: 4px 10px; color: #3fb950; display: inline-block; margin: 3px; cursor: pointer; }
  .bird-det:hover { background: #0d2f0d; }
  .bird-img-card { background: #0d1f0d; border: 1px solid #238636; border-radius: 8px; overflow: hidden; max-width: 200px; margin: 8px 4px; display: inline-block; cursor: zoom-in; transition: max-width 0.25s; }
  .bird-img-card.expanded { max-width: 100%; cursor: zoom-out; }
  .bird-img-card img { width: 100%; display: block; border-radius: 6px 6px 0 0; }
  .bird-img-label { font-size: 0.75rem; color: #3fb950; padding: 4px 8px; }
  .rec-photo { margin-top: 12px; }
  .rec-photo img { max-width: 100%; border-radius: 8px; border: 1px solid #30363d; cursor: zoom-in; }
  .rec-photo img.expanded { cursor: zoom-out; }
  .section { font-size: 0.72rem; text-transform: uppercase; color: #8b949e; letter-spacing: 1px; margin: 20px 0 8px; }
</style>
</head>
<body>
<div class="container">
  <a class="back" href="/">&#8592; mimir</a>
  <h2>{{ display_time }}</h2>
  <div class="meta">{{ duration }} &middot; {{ name }}</div>
  <div class="waveform-wrap" onclick="seekAudio(event)">
    <canvas class="waveform-canvas" id="waveform" data-audiopath="/audio/{{ wav_path | urlencode }}"></canvas>
    <div class="playhead" id="playhead"></div>
  </div>
  <div class="player-controls">
    <button class="play-btn" id="play-btn" onclick="togglePlay()">&#9654;</button>
    <button class="stop-btn" onclick="stopAudio()">&#9632;</button>
    <span class="time-display" id="time-display">0:00 / {{ duration }}</span>
  </div>
  <audio id="audio" src="/audio/{{ wav_path | urlencode }}" preload="auto"></audio>
  {% if tags %}
  <div style="margin-bottom:8px">
    {% for t in tags %}
    <span class="tag">{{ t.icon }} {{ t.label }} <span class="tag-conf">{{ (t.confidence*100)|int }}%</span></span>
    {% endfor %}
  </div>
  {% endif %}
  {% if birds %}
  <div class="section">Bird Detections</div>
  <div style="margin-bottom:8px">
    {% for b in birds %}
    {% if b.start is defined %}
    <span class="bird-det" onclick="seekTo({{ b.start }})" title="Jump to {{ b.start }}s">
      {{ b.icon }} <strong>{{ b.label }}</strong> <span class="tag-conf">{{ (b.confidence*100)|int }}%</span>
      <span style="color:#8b949e;font-size:0.75rem">&#9654; {{ b.start }}&ndash;{{ b.end }}s</span>
    </span>
    {% else %}
    <span class="bird-det">{{ b.icon }} <strong>{{ b.label }}</strong> <span class="tag-conf">{{ (b.confidence*100)|int }}%</span></span>
    {% endif %}
    {% endfor %}
  </div>
  {% set birds_with_images = birds | selectattr('image_url', 'defined') | list %}
  {% if birds_with_images %}
  <div>
    {% for b in birds_with_images %}
    <div class="bird-img-card" onclick="this.classList.toggle('expanded')">
      <img src="{{ b.image_url }}" alt="{{ b.label }}" loading="lazy">
      <div class="bird-img-label">{{ b.icon }} {{ b.label }}</div>
    </div>
    {% endfor %}
  </div>
  {% endif %}
  {% endif %}
  {% if photo %}
  <div class="section">Camera Capture</div>
  <div class="rec-photo">
    <img src="/api/camera/photo/{{ photo }}" onclick="this.classList.toggle('expanded')">
  </div>
  {% endif %}
</div>
<script>
const audio = document.getElementById('audio');
const canvas = document.getElementById('waveform');
const playhead = document.getElementById('playhead');
const playBtn = document.getElementById('play-btn');
const timeEl = document.getElementById('time-display');
let peaks = null;
function formatTime(s) {
  if (!s || isNaN(s)) return '0:00';
  return Math.floor(s/60) + ':' + ('0'+Math.floor(s%60)).slice(-2);
}
async function loadWaveform() {
  if (peaks) return peaks;
  try {
    const p = canvas.dataset.audiopath.replace('/audio/','');
    const r = await fetch('/api/waveform/' + p);
    peaks = await r.json();
    drawWaveform(0);
  } catch(e) { peaks = []; }
  return peaks;
}
function drawWaveform(progress) {
  if (!peaks || !peaks.length) return;
  const rect = canvas.getBoundingClientRect();
  if (rect.width === 0) {
    // Canvas not laid out yet, retry
    requestAnimationFrame(() => drawWaveform(progress));
    return;
  }
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 2;
  const w = canvas.width = Math.round(rect.width * dpr);
  const h = canvas.height = Math.round(rect.height * dpr);
  ctx.clearRect(0,0,w,h);
  const barW = Math.max(1, w/peaks.length);
  const mid = h/2;
  for (let i=0; i<peaks.length; i++) {
    const amp = peaks[i]*mid*0.9;
    ctx.fillStyle = (i/peaks.length < progress) ? '#58a6ff' : '#30363d';
    ctx.fillRect(i*barW, mid-amp, barW-1, amp*2||1);
  }
}
function tick() {
  if (audio.paused) return;
  const p = audio.currentTime/(audio.duration||1);
  drawWaveform(p);
  playhead.style.left = (p*100)+'%';
  playhead.style.opacity = '1';
  timeEl.textContent = formatTime(audio.currentTime)+' / '+formatTime(audio.duration);
  requestAnimationFrame(tick);
}
function togglePlay() {
  loadWaveform().then(()=>{
    if (audio.paused) { audio.play(); playBtn.textContent='\u23F8'; tick(); }
    else { audio.pause(); playBtn.textContent='\u25B6'; }
  });
}
function stopAudio() {
  audio.pause(); audio.currentTime=0;
  playBtn.textContent='\u25B6'; playhead.style.opacity='0';
  if (peaks) drawWaveform(0);
  timeEl.textContent='0:00 / '+formatTime(audio.duration||0);
}
function seekTo(sec) {
  loadWaveform().then(()=>{ audio.currentTime=sec; audio.play(); playBtn.textContent='\u23F8'; tick(); });
}
function seekAudio(e) {
  const rect=canvas.getBoundingClientRect();
  const frac=(e.clientX-rect.left)/rect.width;
  loadWaveform().then(()=>{
    audio.currentTime=frac*(audio.duration||0);
    if(audio.paused) drawWaveform(frac);
    timeEl.textContent=formatTime(audio.currentTime)+' / '+formatTime(audio.duration);
  });
}
audio.addEventListener('ended',()=>{ playBtn.textContent='\u25B6'; playhead.style.opacity='0'; if(peaks)drawWaveform(1); });
requestAnimationFrame(() => loadWaveform());
</script>
</body>
</html>
"""

@app.route("/birds/clip/<path:name>")
def public_clip_page(name):
    """Public clip page — only serves clips with bird detections and no speech."""
    cfg = load_config()
    rdir = Path(cfg["recordings_dir"])
    wav = next(rdir.rglob(name), None)
    if not wav:
        abort(404)
    sidecar = wav.with_suffix(".json")
    analysis = {}
    if sidecar.exists():
        try: analysis = json.loads(sidecar.read_text())
        except: pass
    birds = analysis.get("birds", [])
    tags = {t["label"] for t in analysis.get("tags", [])}
    # Block non-bird clips and clips with speech
    if not birds or "speech" in tags:
        abort(403)
    # Reuse the authenticated clip page template
    from datetime import datetime
    import wave as wavelib
    mt = datetime.fromtimestamp(wav.stat().st_mtime)
    safe_tags = [t for t in analysis.get("tags", []) if t.get("source") != "birdnet"]
    try:
        with wavelib.open(str(wav), "r") as wf:
            secs = wf.getnframes() / wf.getframerate()
            duration = f"{int(secs//60)}:{int(secs%60):02d}"
    except Exception:
        duration = "\u2014"
    now = datetime.now()
    if mt.date() == now.date():
        day = "Today"
    elif (now.date() - mt.date()).days == 1:
        day = "Yesterday"
    else:
        day = mt.strftime("%a %b %-d")
    display_time = f"{day} {mt.strftime('%-I:%M %p')}"
    return render_template_string(CLIP_PAGE_TEMPLATE,
        name=name, wav_path=str(wav).lstrip("/"), display_time=display_time,
        duration=duration, birds=birds, tags=safe_tags,
        photo=analysis.get("photo"), video=analysis.get("video"))


@app.route("/clip/<path:name>")
@require_auth
def clip_page(name):
    cfg = load_config()
    rdir = Path(cfg["recordings_dir"])
    wav = next(rdir.rglob(name), None)
    if not wav:
        abort(404)
    sidecar = wav.with_suffix(".json")
    analysis = {}
    if sidecar.exists():
        try: analysis = json.loads(sidecar.read_text())
        except: pass
    from datetime import datetime
    import wave as wavelib
    mt = datetime.fromtimestamp(wav.stat().st_mtime)
    birds = analysis.get("birds", [])
    tags = [t for t in analysis.get("tags", []) if t.get("source") != "birdnet"]
    try:
        with wavelib.open(str(wav), "r") as wf:
            secs = wf.getnframes() / wf.getframerate()
            m, s = int(secs // 60), int(secs % 60)
            duration = f"{m}:{s:02d}"
    except Exception:
        duration = "\u2014"
    now = datetime.now()
    if mt.date() == now.date():
        day = "Today"
    elif (now.date() - mt.date()).days == 1:
        day = "Yesterday"
    else:
        day = mt.strftime("%a %b %-d")
    display_time = f"{day} {mt.strftime('%-I:%M %p')}"
    return render_template_string(CLIP_PAGE_TEMPLATE,
        name=name, wav_path=str(wav).lstrip("/"), display_time=display_time,
        duration=duration, birds=birds, tags=tags,
        photo=analysis.get("photo"), video=analysis.get("video"))



@app.route("/api/confirm_bird", methods=["POST"])
def api_confirm_bird():
    data = request.get_json(silent=True) or {}
    path = data.get("path", "")
    label = data.get("label", "")
    correct = data.get("correct", True)
    sidecar = Path(path).with_suffix(".json")
    if sidecar.exists():
        try:
            d = json.loads(sidecar.read_text())
            feedback = d.setdefault("feedback", {})
            confirmed = set(feedback.get("confirmed", []))
            denied = set(feedback.get("denied", []))
            if correct:
                confirmed.add(label)
                denied.discard(label)
            else:
                denied.add(label)
                confirmed.discard(label)
            feedback["confirmed"] = list(confirmed)
            feedback["denied"] = list(denied)
            sidecar.write_text(json.dumps(d, indent=2))
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/queue")
@require_auth
def api_queue():
    try:
        import sys
        from pathlib import Path as P
        sys.path.insert(0, str(P("/home/pi/mimir")))
        from analysis import analyzer
        return jsonify(analyzer.queue_status())
    except Exception as e:
        return jsonify({"current": None, "pending_count": 0, "pending": [], "error": str(e)})

@app.route("/api/disk")
@require_auth
def api_disk():
    import shutil
    cfg = load_config()
    rdir = Path(cfg["recordings_dir"])
    rec_bytes = sum(f.stat().st_size for f in rdir.rglob("*.wav")) if rdir.exists() else 0
    try:
        usage = shutil.disk_usage(str(rdir))
        device_total, device_free = usage.total, usage.free
    except Exception:
        device_total = device_free = 0
    # Find mount point
    mount = str(rdir.resolve())
    p = Path(mount)
    while not p.is_mount():
        p = p.parent
    mount = str(p)
    # Check if on same device as / (SD card on Pi)
    try:
        on_sd = os.stat("/").st_dev == os.stat(str(rdir)).st_dev
    except Exception:
        on_sd = None
    return jsonify({
        "recordings_bytes": rec_bytes,
        "device_total": device_total,
        "device_free": device_free,
        "mount": mount,
        "on_sd": on_sd,
    })


@app.route("/api/recordings_html")
@require_auth
def api_recordings_html():
    page = int(request.args.get("page", 1))
    tag_filter = request.args.get("filter", "all")
    recs, total, total_pages, page = paginate(get_recordings(tag_filter), page)
    html = "".join(render_template_string(RECORDING_ITEM_TEMPLATE, rec=rec) for rec in recs)
    return jsonify({"html": html, "page": page, "total_pages": total_pages, "total": total, "per_page": PER_PAGE})


@app.route("/live")
@require_auth
def live_audio():
    @stream_with_context
    def generate():
        try:
            sock = socket_lib.socket(socket_lib.AF_UNIX, socket_lib.SOCK_STREAM)
            sock.connect(str(LIVE_SOCKET))
            while True:
                data = sock.recv(8820)
                if not data:
                    break
                yield data
        except Exception:
            pass
    return Response(generate(), mimetype="application/octet-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/settings", methods=["POST"])
@require_auth
def settings():
    cfg = load_config()
    cfg["threshold_multiplier"] = float(request.form["threshold_multiplier"])
    cfg["pre_roll_seconds"] = float(request.form["pre_roll_seconds"])
    cfg["post_roll_seconds"] = float(request.form["post_roll_seconds"])
    cfg["max_duration_seconds"] = float(request.form["max_duration_seconds"])
    cfg["continuous_chunk_seconds"] = float(request.form["continuous_chunk_seconds"])
    cfg["round_robin"] = "round_robin" in request.form
    cfg["max_recordings_gb"] = float(request.form.get("max_recordings_gb", 10))
    cfg["analysis_enabled"] = "analysis_enabled" in request.form
    cfg["whisper_enabled"] = "whisper_enabled" in request.form
    cfg["whisper_model"] = request.form.get("whisper_model", "tiny")
    save_config(cfg)
    return redirect(url_for("index"))


@app.route("/calibrate", methods=["POST"])
@require_auth
def calibrate():
    Path("/run/mimir/calibrate.trigger").touch()
    return redirect(url_for("index"))


@app.route("/monitoring", methods=["POST"])
def set_monitoring():
    cfg = load_config()
    cfg["monitoring_enabled"] = request.json.get("enabled", True)
    save_config(cfg)
    return jsonify({"monitoring_enabled": cfg["monitoring_enabled"]})


@app.route("/mode", methods=["POST"])
def set_mode():
    cfg = load_config()
    cfg["mode"] = request.json.get("mode", "event")
    save_config(cfg)
    return jsonify({"mode": cfg["mode"]})


@app.route("/audio/<path:filepath>")
def audio(filepath):
    return send_file("/" + filepath, mimetype="audio/wav")


@app.route("/api/camera/photo/<path:fname>")
def camera_photo(fname):
    """Serve a camera clip thumbnail or video from local storage."""
    # Check /mnt/usb/camera/ tree
    camera_dir = Path("/mnt/usb/camera")
    # fname could be full path or just filename
    p = Path(fname)
    if p.exists():
        fpath = p
    else:
        fpath = next(camera_dir.rglob(p.name), None) if p.name else None
        if not fpath:
            fpath = camera_dir / fname
    if not fpath or not fpath.exists():
        abort(404)
    mime = "video/mp4" if fpath.suffix == ".mp4" else "image/jpeg"
    return send_file(str(fpath), mimetype=mime)


@app.route("/api/camera/clips")
@require_auth
def camera_clips():
    """Return recent camera clips as JSON."""
    camera_dir = Path("/mnt/usb/camera")
    clips = []
    for mp4 in sorted(camera_dir.rglob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)[:50]:
        thumb = mp4.with_name(mp4.stem + "_thumb.jpg")
        sidecar = mp4.with_suffix(".json")
        meta = {}
        if sidecar.exists():
            try: meta = json.loads(sidecar.read_text())
            except: pass
        from datetime import datetime
        mt = datetime.fromtimestamp(mp4.stat().st_mtime)
        now = datetime.now()
        if mt.date() == now.date():
            day = "Today"
        elif (now.date() - mt.date()).days == 1:
            day = "Yesterday"
        else:
            day = mt.strftime("%a %b %-d")
        vision = meta.get("vision_id", {})
        clips.append({
            "path": str(mp4),
            "name": mp4.name,
            "display_time": f"{day} {mt.strftime('%-I:%M %p')}",
            "thumb": str(thumb) if thumb.exists() else None,
            "label": meta.get("label", "motion"),
            "confidence": meta.get("confidence", 0),
            "size_kb": round(mp4.stat().st_size / 1024, 1),
            "vision_species": vision.get("species", ""),
            "vision_confidence": vision.get("confidence", ""),
            "vision_desc": vision.get("description", ""),
        })
    return jsonify(clips)



CAMERA_FEED_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mimir — camera feed</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', monospace; background: #0d1117; color: #c9d1d9; min-height: 100vh; }
  .container { max-width: 900px; margin: 0 auto; padding: 20px; }
  h1 { color: #58a6ff; font-size: 1.1rem; margin-bottom: 4px; }
  .subtitle { color: #8b949e; font-size: 0.8rem; margin-bottom: 20px; }
  a.back { color: #8b949e; font-size: 0.85rem; text-decoration: none; }
  a.back:hover { color: #58a6ff; }
  .clips-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .clip-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
  .clip-card video { width: 100%; display: block; background: #0a0e14; }
  .clip-card img.thumb { width: 100%; display: block; cursor: pointer; background: #0a0e14; }
  .clip-info { padding: 10px; }
  .clip-time { font-size: 0.85rem; color: #e6edf3; font-weight: 600; }
  .clip-label { display: inline-block; font-size: 0.75rem; background: #0d1f0d; border: 1px solid #238636; border-radius: 8px; padding: 1px 8px; color: #3fb950; margin-top: 4px; }
  .clip-label.person { background: #1f6feb22; border-color: #1f6feb; color: #58a6ff; }
  .clip-label.motion { background: #21262d; border-color: #30363d; color: #8b949e; }
  .clip-meta { font-size: 0.72rem; color: #8b949e; margin-top: 4px; }
  .none { color: #8b949e; font-size: 0.85rem; padding: 24px 0; }
  .snapshot { margin-bottom: 16px; }
  .snapshot img { width: 100%; max-width: 640px; border-radius: 8px; border: 1px solid #30363d; }
</style>
</head>
<body>
<div class="container">
  <a class="back" href="/">&#8592; mimir</a>
  <h1>camera feed</h1>
  <div class="subtitle">Motion and AI detections from Reolink · <a href="/api/camera/snapshot" target="_blank" style="color:#58a6ff;text-decoration:none">Live snapshot</a></div>

  {% if clips %}
  <div class="clips-grid">
    {% for c in clips %}
    <div class="clip-card">
      {% if c.thumb %}
      <img class="thumb" src="/api/camera/photo/{{ c.thumb }}" alt="{{ c.label }}"
           onclick="this.style.display='none';this.nextElementSibling.style.display='block';this.nextElementSibling.play()">
      <video src="/api/camera/photo/{{ c.path }}" controls preload="none" style="display:none"></video>
      {% else %}
      <video src="/api/camera/photo/{{ c.path }}" controls preload="metadata"></video>
      {% endif %}
      <div class="clip-info">
        <div class="clip-time">{{ c.display_time }}</div>
        <span class="clip-label {% if 'person' in c.label %}person{% elif 'motion' in c.label %}motion{% endif %}">
          {% if 'animal' in c.label %}&#x1F426;{% elif 'person' in c.label %}&#x1F464;{% else %}&#x25CE;{% endif %}
          {{ c.label }} {{ (c.confidence * 100)|int }}%
        </span>
        {% if c.vision_species %}
        <span class="clip-label" style="background:#1a1a2e;border-color:#6f42c1;color:#d2a8ff">
          &#x1F441; {{ c.vision_species }} ({{ c.vision_confidence }})
        </span>
        {% if c.vision_desc %}<div style="font-size:0.7rem;color:#8b949e;margin-top:2px">{{ c.vision_desc }}</div>{% endif %}
        {% endif %}
        <div class="clip-meta">{{ c.size_kb }} KB
          <button onclick="deleteClip(this, '{{ c.path }}')" style="margin-left:8px;padding:1px 6px;font-size:0.7rem;color:#f85149;border:1px solid #da3633;background:#da363311;border-radius:4px;cursor:pointer" title="Delete">&#x2715;</button>
        </div>
      </div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="none">No camera clips yet.</div>
  {% endif %}
</div>
<script>
async function deleteClip(btn, path) {
  const card = btn.closest('.clip-card');
  btn.disabled = true;
  btn.textContent = '...';
  const r = await fetch('/api/camera/delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path})
  });
  if (r.ok) {
    card.style.transition = 'opacity 0.3s';
    card.style.opacity = '0';
    setTimeout(() => card.remove(), 300);
  } else {
    btn.disabled = false;
    btn.textContent = '\u2715';
  }
}
</script>
</body>
</html>
"""




@app.route("/api/camera/delete", methods=["POST"])
@require_auth
def delete_camera_clip():
    data = request.get_json(silent=True) or {}
    path = data.get("path", "")
    if not path:
        return jsonify({"error": "no path"}), 400
    p = Path(path)
    camera_dir = Path("/mnt/usb/camera")
    if not str(p).startswith(str(camera_dir)):
        return jsonify({"error": "invalid path"}), 403
    deleted = []
    for ext in [".mp4", ".json", "_thumb.jpg"]:
        f = p.with_suffix(ext) if ext != "_thumb.jpg" else p.with_name(p.stem + "_thumb.jpg")
        if f.exists():
            f.unlink()
            deleted.append(f.name)
    # Also clean crop file
    crop = p.with_name(p.stem + "_crop.jpg")
    if crop.exists():
        crop.unlink()
        deleted.append(crop.name)
    # Remove references from audio sidecars pointing to this clip
    rdir = Path(load_config().get("recordings_dir", "/mnt/usb"))
    clip_str = str(p)
    thumb_str = str(p.with_name(p.stem + "_thumb.jpg"))
    for sc in rdir.rglob("*.json"):
        try:
            d = json.loads(sc.read_text())
            changed = False
            if d.get("video") and str(d["video"]) == clip_str:
                del d["video"]
                changed = True
            if d.get("photo") and str(d["photo"]) == thumb_str:
                del d["photo"]
                changed = True
            if changed:
                sc.write_text(json.dumps(d, indent=2))
        except Exception:
            pass
    # Clean empty day dirs
    try:
        p.parent.rmdir()
    except OSError:
        pass
    return jsonify({"deleted": deleted})



@app.route("/crows")
@require_auth
def crows_page():
    import sys
    sys.path.insert(0, "/home/pi/mimir")
    from crow_id import get_all_crows, get_crow_sightings
    from datetime import datetime
    from collections import defaultdict

    crows = get_all_crows()
    cfg = load_config()
    rdir = Path(cfg["recordings_dir"])

    for c in crows:
        c["sightings"] = get_crow_sightings(c["id"], limit=100)

        # Format dates
        for field in ["first_seen", "last_seen"]:
            if c.get(field):
                try:
                    dt = datetime.fromisoformat(c[field])
                    now = datetime.now()
                    if dt.date() == now.date():
                        c[field + "_display"] = f"Today {dt.strftime('%-I:%M %p')}"
                    elif (now.date() - dt.date()).days == 1:
                        c[field + "_display"] = f"Yesterday {dt.strftime('%-I:%M %p')}"
                    else:
                        c[field + "_display"] = dt.strftime("%b %-d, %-I:%M %p")
                except:
                    c[field + "_display"] = c[field][:16]
            else:
                c[field + "_display"] = "\u2014"

        # Hourly heatmap from sightings
        hourly = [0] * 24
        daily_counts = defaultdict(int)
        for s in c["sightings"]:
            try:
                dt = datetime.fromisoformat(s["timestamp"])
                hourly[dt.hour] += 1
                daily_counts[dt.strftime("%Y-%m-%d")] += 1
            except:
                pass
        max_h = max(hourly) or 1
        c["hourly"] = [{"hour": h, "count": hourly[h],
                        "label": f"{h%12 or 12}{'am' if h<12 else 'pm'}",
                        "alpha": round(0.15 + 0.85 * hourly[h] / max_h, 2) if hourly[h] else 0}
                       for h in range(24)]
        c["days_active"] = len(daily_counts)
        c["avg_per_day"] = round(sum(daily_counts.values()) / max(len(daily_counts), 1), 1)

        # Find latest camera clip that has a confirmed crow/corvid in its sidecar
        c["photo"] = None
        camera_dir = Path("/mnt/usb/camera")
        if camera_dir.exists():
            for clip_json in sorted(camera_dir.rglob("*.json"),
                                     key=lambda f: f.stat().st_mtime, reverse=True):
                try:
                    meta = json.loads(clip_json.read_text())
                    label = meta.get("label", "")
                    vision = meta.get("vision_id", {})
                    vision_species = (vision.get("species", "") or "").lower()
                    # Match if label or vision ID contains a corvid reference
                    corvid_words = {"crow", "raven", "jay", "magpie", "corvid"}
                    if any(w in label.lower() for w in corvid_words) or \
                       any(w in vision_species for w in corvid_words):
                        thumb = clip_json.with_name(clip_json.stem + "_thumb.jpg")
                        if thumb.exists():
                            c["photo"] = str(thumb)
                            break
                except:
                    pass

        # Format sightings for display
        for s in c["sightings"]:
            try:
                dt = datetime.fromisoformat(s["timestamp"])
                now = datetime.now()
                if dt.date() == now.date():
                    s["display_time"] = f"Today {dt.strftime('%-I:%M %p')}"
                elif (now.date() - dt.date()).days == 1:
                    s["display_time"] = f"Yesterday {dt.strftime('%-I:%M %p')}"
                else:
                    s["display_time"] = dt.strftime("%b %-d %-I:%M %p")
            except:
                s["display_time"] = s["timestamp"][:16]

    return render_template_string(CROWS_TEMPLATE, crows=crows)


CROWS_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mimir \u2014 known corvids</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', monospace; background: #0d1117; color: #c9d1d9; min-height: 100vh; }
  .container { max-width: 800px; margin: 0 auto; padding: 20px; }
  h1 { color: #58a6ff; font-size: 1.1rem; margin-bottom: 16px; }
  a.back { color: #8b949e; font-size: 0.85rem; text-decoration: none; }
  a.back:hover { color: #58a6ff; }

  .corvid-card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; margin-bottom: 16px; overflow: hidden; }
  .corvid-header { display: flex; gap: 16px; padding: 16px; align-items: flex-start; }
  .corvid-photo { width: 120px; height: 90px; border-radius: 8px; object-fit: cover; border: 1px solid #30363d; background: #0d1117; flex-shrink: 0; }
  .corvid-photo-placeholder { width: 120px; height: 90px; border-radius: 8px; border: 1px solid #30363d; background: #0d1117; display: flex; align-items: center; justify-content: center; font-size: 2.5rem; flex-shrink: 0; }
  .corvid-info { flex: 1; min-width: 0; }
  .corvid-name { font-size: 1.1rem; font-weight: 600; color: #d2a8ff; margin-bottom: 2px; }
  .corvid-name input { background: transparent; border: 1px solid transparent; border-radius: 4px; color: #d2a8ff; font-family: inherit; font-size: 1.1rem; font-weight: 600; padding: 1px 4px; width: 200px; }
  .corvid-name input:hover { border-color: #30363d; }
  .corvid-name input:focus { border-color: #6f42c1; outline: none; background: #0d1117; }
  .corvid-species { font-size: 0.8rem; color: #8b949e; }
  .corvid-stats { display: flex; gap: 16px; margin-top: 8px; flex-wrap: wrap; }
  .stat { text-align: center; }
  .stat .num { font-size: 1.3rem; font-weight: 600; color: #e6edf3; }
  .stat .lbl { font-size: 0.65rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }

  .corvid-section { padding: 0 16px 12px; }
  .section-label { font-size: 0.7rem; text-transform: uppercase; color: #8b949e; letter-spacing: 1px; margin-bottom: 6px; }

  .notes-input { width: 100%; background: #0d1117; border: 1px solid #21262d; border-radius: 6px; color: #c9d1d9; font-family: inherit; font-size: 0.8rem; padding: 6px 8px; resize: vertical; min-height: 32px; }
  .notes-input:focus { border-color: #6f42c1; outline: none; }

  .heatmap { display: grid; grid-template-columns: repeat(24, 1fr); gap: 2px; }
  .hm-cell { height: 22px; border-radius: 2px; background: #21262d; position: relative; }
  .hm-cell:hover::after { content: attr(data-tip); position: absolute; bottom: 26px; left: 50%; transform: translateX(-50%); background: #161b22; border: 1px solid #30363d; border-radius: 4px; padding: 2px 6px; font-size: 0.65rem; white-space: nowrap; color: #c9d1d9; z-index: 10; }
  .hm-labels { display: grid; grid-template-columns: repeat(24, 1fr); gap: 2px; margin-top: 1px; }
  .hm-label { font-size: 0.55rem; color: #484f58; text-align: center; }

  .sightings-list { max-height: 300px; overflow-y: auto; }
  .sighting-row { display: flex; align-items: center; gap: 10px; padding: 4px 0; border-bottom: 1px solid #21262d; font-size: 0.78rem; }
  .sighting-row:last-child { border-bottom: none; }
  .sighting-time { color: #8b949e; min-width: 140px; }
  .sighting-conf { color: #3fb950; min-width: 40px; }
  .sighting-link { color: #58a6ff; text-decoration: none; }
  .sighting-link:hover { text-decoration: underline; }

  .none { color: #8b949e; font-size: 0.85rem; padding: 24px 0; }

  @media (max-width: 640px) {
    .corvid-header { flex-direction: column; align-items: center; text-align: center; }
    .corvid-stats { justify-content: center; }
    .corvid-name input { text-align: center; }
  }
</style>
</head>
<body>
<div class="container">
  <a href="/" style="display:inline-block;padding:8px 14px;margin-bottom:8px;font-size:0.85rem;color:#58a6ff;background:#1f6feb11;border:1px solid #1f6feb;border-radius:8px;text-decoration:none">&#8592; mimir</a>
  <h1>&#x1F426;&#x200D;&#x2B1B; Known Corvids</h1>

  {% if crows %}
  {% for c in crows %}
  <div class="corvid-card">
    <div class="corvid-header">
      {% if c.photo %}
      <img class="corvid-photo" src="/api/camera/photo/{{ c.photo }}" alt="{{ c.name }}">
      {% else %}
      <div class="corvid-photo-placeholder">&#x1F426;&#x200D;&#x2B1B;</div>
      {% endif %}

      <div class="corvid-info">
        <div class="corvid-name">
          <input value="{{ c.name }}" onchange="renameCrow({{ c.id }}, this.value)" title="Click to rename">
        </div>
        <div class="corvid-species">{{ c.species }} &middot; ID #{{ c.id }}</div>
        <div class="corvid-stats">
          <div class="stat"><div class="num">{{ c.sighting_count }}</div><div class="lbl">sightings</div></div>
          <div class="stat"><div class="num">{{ c.days_active }}</div><div class="lbl">days seen</div></div>
          <div class="stat"><div class="num">{{ c.avg_per_day }}</div><div class="lbl">avg/day</div></div>
        </div>
        <div style="font-size:0.75rem;color:#8b949e;margin-top:6px">
          First: {{ c.first_seen_display }} &middot; Last: {{ c.last_seen_display }}
        </div>
      </div>
    </div>
    <div class="corvid-section">
      <div class="section-label">Voice Signature</div>
      <img src="/api/crow/voiceprint/{{ c.id }}" alt="voiceprint"
           style="max-width:100%;border-radius:6px;">
    </div>

    <div class="corvid-section">
      <div class="section-label">Notes</div>
      <textarea class="notes-input" placeholder="Add notes about this corvid..."
        onchange="updateNotes({{ c.id }}, this.value)">{{ c.notes or '' }}</textarea>
    </div>

    <div class="corvid-section">
      <div class="section-label">Activity by Hour</div>
      <div class="heatmap">
        {% for h in c.hourly %}
        <div class="hm-cell" data-tip="{{ h.label }}: {{ h.count }}"
          style="background: rgba(111, 66, 193, {{ h.alpha }})"></div>
        {% endfor %}
      </div>
      <div class="hm-labels">
        {% for h in c.hourly %}
        <div class="hm-label">{% if h.hour % 4 == 0 %}{{ h.label }}{% endif %}</div>
        {% endfor %}
      </div>
    </div>

    <div class="corvid-section">
      <div class="section-label">All Sightings ({{ c.sightings|length }})</div>
      <div class="sightings-list">
        {% for s in c.sightings %}
        <div class="sighting-row">
          <span class="sighting-time">{{ s.display_time }}</span>
          <span class="sighting-conf">{{ (s.confidence * 100)|int }}%</span>
          <span style="color:#8b949e">{{ s.start_sec }}&#8211;{{ s.end_sec }}s</span>
          <button onclick="playSegment(this,'{{ s.wav_path.lstrip('/') }}',{{ s.start_sec }},{{ s.end_sec }})"
            style="background:#23863622;border:1px solid #238636;border-radius:4px;color:#3fb950;font-size:0.72rem;padding:2px 8px;cursor:pointer">&#9654; call</button>
          <button onclick="playSegment(this,'{{ s.wav_path.lstrip('/') }}',0,0)"
            style="background:#1f6feb11;border:1px solid #1f6feb;border-radius:4px;color:#58a6ff;font-size:0.72rem;padding:2px 8px;cursor:pointer">&#9654; full</button>
          <audio class="sighting-audio" data-path="{{ s.wav_path.lstrip('/') }}" preload="none" style="display:none"></audio>
        </div>
        {% endfor %}
      </div>
    </div>
  </div>
  {% endfor %}
  {% else %}
  <div class="none">No corvids identified yet. When BirdNET detects corvids, their voice fingerprints will be recorded here.</div>
  {% endif %}
</div>
<script>
let activeAudio = null;
function playSegment(btn, path, start, end) {
  // Stop any currently playing audio
  if (activeAudio && !activeAudio.paused) {
    activeAudio.pause();
    activeAudio.currentTime = 0;
    if (activeAudio._btn) activeAudio._btn.textContent = '\u25B6 ' + activeAudio._btn.dataset.label;
  }
  const row = btn.closest('.sighting-row');
  let audio = row.querySelector('.sighting-audio');
  if (!audio.src || !audio.src.includes(path)) {
    audio.src = '/audio/' + path;
  }
  const label = end > 0 ? 'call' : 'full';
  btn.dataset.label = label;
  if (end > 0) {
    audio.currentTime = start;
    audio.play();
    btn.textContent = '\u23F8 call';
    // Stop at end time
    audio._stopAt = end;
    audio.ontimeupdate = function() {
      if (this.currentTime >= this._stopAt) {
        this.pause();
        btn.textContent = '\u25B6 call';
      }
    };
  } else {
    audio.currentTime = 0;
    audio.play();
    btn.textContent = '\u23F8 full';
    audio.ontimeupdate = null;
  }
  audio._btn = btn;
  activeAudio = audio;
  audio.onended = function() { btn.textContent = '\u25B6 ' + label; };
  audio.onpause = function() { if (!audio.ontimeupdate) btn.textContent = '\u25B6 ' + label; };
}

async function renameCrow(id, name) {
  await fetch('/api/crow/rename', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id, name})
  });
}
async function updateNotes(id, notes) {
  await fetch('/api/crow/notes', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id, notes})
  });
}
</script>
</body>
</html>
"""



@app.route("/api/crow/rename", methods=["POST"])
@require_auth
def api_crow_rename():
    import sys
    sys.path.insert(0, "/home/pi/mimir")
    from crow_id import rename_crow
    data = request.get_json(silent=True) or {}
    crow_id = data.get("id")
    name = data.get("name", "").strip()
    if crow_id and name:
        rename_crow(crow_id, name)
        return jsonify({"ok": True})
    return jsonify({"error": "missing id or name"}), 400


@app.route("/api/crow/notes", methods=["POST"])
@require_auth
def api_crow_notes():
    import sqlite3
    data = request.get_json(silent=True) or {}
    crow_id = data.get("id")
    notes = data.get("notes", "")
    if crow_id:
        conn = sqlite3.connect("/mnt/usb/crow_id.db")
        conn.execute("UPDATE crows SET notes = ? WHERE id = ?", (notes, crow_id))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    return jsonify({"error": "missing id"}), 400


@app.route("/api/crow/voiceprint/<int:crow_id>")
@require_auth
def api_crow_voiceprint(crow_id):
    """Serve cached spectrogram PNG for this corvid."""
    cached = Path("/mnt/usb/cache/spectrograms") / f"crow_{crow_id}.png"
    if cached.exists():
        return send_file(str(cached), mimetype="image/png")
    abort(404)


@app.route("/camera_feed")
@require_auth
def camera_feed():
    """Camera clips page — motion and AI detections."""
    camera_dir = Path("/mnt/usb/camera")
    clips = []
    for mp4 in sorted(camera_dir.rglob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)[:100]:
        thumb = mp4.with_name(mp4.stem + "_thumb.jpg")
        sidecar = mp4.with_suffix(".json")
        meta = {}
        if sidecar.exists():
            try: meta = json.loads(sidecar.read_text())
            except: pass
        from datetime import datetime
        mt = datetime.fromtimestamp(mp4.stat().st_mtime)
        now = datetime.now()
        if mt.date() == now.date():
            day = "Today"
        elif (now.date() - mt.date()).days == 1:
            day = "Yesterday"
        else:
            day = mt.strftime("%a %b %-d")
        vision = meta.get("vision_id", {})
        clips.append({
            "path": str(mp4),
            "name": mp4.name,
            "display_time": f"{day} {mt.strftime('%-I:%M %p')}",
            "thumb": str(thumb) if thumb.exists() else None,
            "label": meta.get("label", "motion"),
            "confidence": meta.get("confidence", 0),
            "size_kb": round(mp4.stat().st_size / 1024, 1),
            "vision_species": vision.get("species", ""),
            "vision_confidence": vision.get("confidence", ""),
            "vision_desc": vision.get("description", ""),
        })
    return render_template_string(CAMERA_FEED_TEMPLATE, clips=clips)


@app.route("/api/camera/snapshot")
@require_auth
def camera_snapshot():
    """Grab a live JPEG from the RTSP stream."""
    import subprocess
    cfg = load_config()
    rtsp_url = cfg.get("rtsp_url", "")
    if not rtsp_url:
        abort(503)
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    try:
        subprocess.run([
            "ffmpeg", "-y", "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-frames:v", "1", "-q:v", "3", "-update", "1",
            tmp.name,
        ], timeout=10, capture_output=True)
        if os.path.exists(tmp.name) and os.path.getsize(tmp.name) > 5000:
            return send_file(tmp.name, mimetype="image/jpeg")
    except Exception:
        pass
    abort(503)

@app.route("/api/waveform/<path:filepath>")
def waveform(filepath):
    import wave as wavelib
    import numpy as np
    p = Path("/" + filepath)
    if not p.exists():
        return jsonify([])
    try:
        with wavelib.open(str(p), "r") as wf:
            n_frames = wf.getnframes()
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            raw = wf.readframes(n_frames)

        # Decode to int16 mono
        if sampwidth == 2:
            samples = np.frombuffer(raw, dtype=np.int16)
        else:
            samples = np.frombuffer(raw, dtype=np.int8).astype(np.int16)

        if n_channels > 1:
            samples = samples[::n_channels]

        # Downsample to ~800 points: take peak within each bucket
        target = 800
        samples = samples.astype(np.float32) / 32768.0
        bucket = max(1, len(samples) // target)
        trimmed = samples[:len(samples) - (len(samples) % bucket)]
        buckets = trimmed.reshape(-1, bucket)
        peaks = np.max(np.abs(buckets), axis=1)
        max_val = peaks.max()
        if max_val > 0:
            peaks = peaks / max_val  # normalize so loudest bar = full height
        return jsonify([round(v, 4) for v in peaks.tolist()])
    except Exception as e:
        return jsonify([])


@app.route("/delete", methods=["POST"])
@require_auth
def delete_recording():
    filepath = request.form.get("path")
    deleted = False
    if filepath:
        p = Path(filepath)
        cfg = load_config()
        # Safety: only delete files inside recordings_dir
        if p.exists() and str(p).startswith(cfg["recordings_dir"]):
            p.unlink()
            deleted = True
            # Remove sidecar if present
            sidecar = p.with_suffix(".json")
            if sidecar.exists():
                # Clean up crow_id sightings linked to this wav
                try:
                    import sqlite3
                    crow_db = Path("/mnt/usb/crow_id.db")
                    if crow_db.exists():
                        cdb = sqlite3.connect(str(crow_db))
                        cdb.execute("DELETE FROM sightings WHERE wav_path = ?", (str(p),))
                        # Remove any crows with zero sightings left
                        cdb.execute("DELETE FROM crows WHERE id NOT IN (SELECT DISTINCT crow_id FROM sightings)")
                        cdb.commit()
                        cdb.close()
                except Exception:
                    pass
                sidecar.unlink()
            # Remove empty day dirs
            try:
                p.parent.rmdir()
            except OSError:
                pass
    # Return JSON for fetch calls, redirect for legacy form POSTs
    if request.headers.get("Accept", "").startswith("application/json") or request.form.get("_fetch"):
        return jsonify({"deleted": deleted})
    ref = request.referrer or url_for("index")
    return redirect(ref)


@app.route("/delete_all", methods=["POST"])
@require_auth
def delete_all():
    cfg = load_config()
    rdir = Path(cfg["recordings_dir"])
    for wav in rdir.rglob("*.wav"):
        wav.unlink()
    for d in sorted(rdir.iterdir(), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass
    ref = request.referrer or url_for("index")
    return redirect(ref)


def get_tailscale_ip():
    """Return the Tailscale IPv4 address, or None if not connected."""
    import subprocess
    try:
        out = subprocess.check_output(["tailscale", "ip", "-4"], text=True).strip()
        return out if out else None
    except Exception:
        return None


STATS_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mimir — bird stats</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', monospace; background: #0d1117; color: #c9d1d9; min-height: 100vh; }
  header { background: #161b22; border-bottom: 1px solid #30363d; padding: 16px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 1.2rem; color: #58a6ff; }
  a.back { color: #8b949e; font-size: 0.85rem; text-decoration: none; margin-left: auto; }
  a.back:hover { color: #58a6ff; }
  .container { max-width: 960px; margin: 0 auto; padding: 24px; }
  .period-tabs { display: flex; gap: 8px; margin-bottom: 24px; }
  .ptab { padding: 6px 18px; border-radius: 20px; border: 1px solid #30363d; background: #21262d; color: #8b949e; cursor: pointer; font-family: inherit; font-size: 0.85rem; text-decoration: none; }
  .ptab.active { background: #1f6feb22; border-color: #1f6feb; color: #58a6ff; }
  .section-title { font-size: 0.75rem; text-transform: uppercase; color: #8b949e; letter-spacing: 1px; margin: 24px 0 12px; }
  .leaderboard { display: grid; gap: 8px; }
  .lb-row { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px 16px; display: flex; align-items: center; gap: 12px; }
  .lb-rank { font-size: 1.1rem; width: 28px; text-align: center; color: #8b949e; }
  .lb-icon { font-size: 1.4rem; }
  .lb-label { flex: 1; font-size: 0.95rem; color: #e6edf3; }
  .lb-sci { font-size: 0.72rem; color: #8b949e; }
  .lb-bar-wrap { flex: 2; background: #21262d; border-radius: 4px; height: 10px; overflow: hidden; }
  .lb-bar { height: 100%; border-radius: 4px; background: linear-gradient(90deg, #238636, #3fb950); }
  .lb-count { font-size: 1rem; font-weight: 600; color: #3fb950; min-width: 48px; text-align: right; }
  .lb-conf { font-size: 0.72rem; color: #8b949e; min-width: 48px; text-align: right; }
  .daily-table { width: 100%; border-collapse: collapse; }
  .daily-table th { text-align: left; font-size: 0.72rem; text-transform: uppercase; color: #8b949e; letter-spacing: 1px; padding: 6px 10px; border-bottom: 1px solid #30363d; }
  .daily-table td { padding: 8px 10px; border-bottom: 1px solid #21262d; font-size: 0.85rem; vertical-align: middle; }
  .daily-table tr:hover td { background: #161b22; }
  .day-label { color: #58a6ff; font-size: 0.85rem; }
  .species-chip { display: inline-flex; align-items: center; gap: 4px; background: #0d1f0d; border: 1px solid #238636; border-radius: 10px; padding: 1px 8px; font-size: 0.75rem; color: #3fb950; margin: 2px; }
  .chip-count { background: #238636; border-radius: 8px; padding: 0 5px; font-size: 0.7rem; color: white; font-weight: 600; }
  .guess-box { background: #161b22; border: 1px solid #d29922; border-radius: 8px; padding: 20px; margin-bottom: 24px; }
  .guess-box h2 { font-size: 0.75rem; text-transform: uppercase; color: #d29922; letter-spacing: 1px; margin-bottom: 12px; }
  .guess-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; margin-top: 12px; }
  .guess-card { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 12px; text-align: center; }
  .guess-card .bird-name { font-size: 0.8rem; color: #8b949e; margin-bottom: 4px; }
  .guess-card .today-count { font-size: 2rem; font-weight: 600; color: #3fb950; }
  .guess-card .week-avg { font-size: 0.75rem; color: #8b949e; margin-top: 4px; }
  .total-events-row { display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
  .stat-bubble { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px 20px; text-align: center; }
  .stat-bubble .num { font-size: 1.8rem; font-weight: 600; color: #e6edf3; }
  .stat-bubble .lbl { font-size: 0.72rem; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-top: 2px; }
  .heatmap { display: grid; grid-template-columns: repeat(24, 1fr); gap: 3px; margin: 8px 0; }
  .hm-cell { height: 28px; border-radius: 3px; background: #21262d; position: relative; cursor: default; }
  .hm-cell:hover::after { content: attr(data-tip); position: absolute; bottom: 32px; left: 50%; transform: translateX(-50%); background: #161b22; border: 1px solid #30363d; border-radius: 4px; padding: 3px 8px; font-size: 0.7rem; white-space: nowrap; color: #c9d1d9; pointer-events: none; z-index: 10; }
  .hm-hour { font-size: 0.6rem; color: #8b949e; text-align: center; }
</style>
</head>
<body>
<header>
  <h1>🦉 mimir — bird stats</h1>
  <a class="back" href="/birds">← back to birds</a>
</header>
<div class="container">

<div class="period-tabs">
  <a class="ptab {% if period == 'day' %}active{% endif %}" href="/stats?period=day">Today</a>
  <a class="ptab {% if period == 'week' %}active{% endif %}" href="/stats?period=week">This Week</a>
  <a class="ptab {% if period == 'month' %}active{% endif %}" href="/stats?period=month">This Month</a>
  <a class="ptab {% if period == 'all' %}active{% endif %}" href="/stats?period=all">All Time</a>
</div>

<div class="total-events-row">
  <div class="stat-bubble"><div class="num">{{ total_detections }}</div><div class="lbl">bird detections</div></div>
  <div class="stat-bubble"><div class="num">{{ total_recordings }}</div><div class="lbl">recordings analyzed</div></div>
  <div class="stat-bubble"><div class="num">{{ species_count }}</div><div class="lbl">species detected</div></div>
  <div class="stat-bubble"><div class="num">{{ days_active }}</div><div class="lbl">days active</div></div>
</div>

{% if top_species %}
<div class="section-title">Top Species — {{ period_label }}</div>
<div class="leaderboard">
  {% for i, s in top_species %}
  <div class="lb-row">
    <div class="lb-rank">{% if i == 1 %}🥇{% elif i == 2 %}🥈{% elif i == 3 %}🥉{% else %}{{ i }}{% endif %}</div>
    <div class="lb-icon">{{ s.icon }}</div>
    <div class="lb-label">
      {{ s.label }}<br>
      <span class="lb-sci">{{ s.scientific }}</span>
    </div>
    <div class="lb-bar-wrap"><div class="lb-bar" style="width:{{ s.pct }}%"></div></div>
    <div class="lb-count">{{ s.count }}×</div>
    <div class="lb-conf">avg {{ s.avg_conf }}%</div>
  </div>
  {% endfor %}
</div>
{% else %}
<div style="color:#8b949e;font-size:0.85rem;padding:16px 0">No bird detections in this period yet.</div>
{% endif %}

{% if hourly_data %}
<div class="section-title">Activity by Hour — {{ period_label }}</div>
<div class="heatmap">
  {% for h in hourly_data %}
  <div class="hm-cell" data-tip="{{ h.hour_label }}: {{ h.count }} detection{{ 's' if h.count != 1 else '' }}"
    style="background: rgba(35,134,54,{{ h.alpha }})"></div>
  {% endfor %}
</div>
<div class="heatmap" style="margin-top:2px">
  {% for h in hourly_data %}
  <div class="hm-hour">{% if h.hour % 6 == 0 %}{{ h.hour_label }}{% endif %}</div>
  {% endfor %}
</div>
{% endif %}

{% if daily_rows %}
<div class="section-title">Daily Breakdown</div>
<table class="daily-table">
  <thead>
    <tr>
      <th>Date</th>
      <th>Detections</th>
      <th>Species heard</th>
    </tr>
  </thead>
  <tbody>
  {% for row in daily_rows %}
    <tr>
      <td class="day-label">{{ row.date }}</td>
      <td style="color:#e6edf3;font-weight:600">{{ row.total }}</td>
      <td>
        {% for sp in row.species %}
        <span class="species-chip">{{ sp.icon }} {{ sp.label }} <span class="chip-count">{{ sp.count }}</span></span>
        {% endfor %}
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% endif %}

{% if guess_species %}
<div style="margin-top:32px"></div>
<div class="guess-box">
  <h2>🎲 Today's Guessing Game — how many calls today?</h2>
  <div style="font-size:0.8rem;color:#8b949e">Share with friends: who can guess the closest total crow calls for today?</div>
  <div class="guess-grid">
    {% for s in guess_species %}
    <div class="guess-card">
      <div style="font-size:1.8rem">{{ s.icon }}</div>
      <div class="bird-name">{{ s.label }}</div>
      <div class="today-count">{{ s.today }}</div>
      <div class="week-avg">7-day avg: {{ s.week_avg }}</div>
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}

</div>
<script>
async function deleteClip(btn, path) {
  const card = btn.closest('.clip-card');
  btn.disabled = true;
  btn.textContent = '...';
  const r = await fetch('/api/camera/delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path})
  });
  if (r.ok) {
    card.style.transition = 'opacity 0.3s';
    card.style.opacity = '0';
    setTimeout(() => card.remove(), 300);
  } else {
    btn.disabled = false;
    btn.textContent = '\u2715';
  }
}
</script>
</body>
</html>
"""


@app.route("/stats")
def stats_page():
    from datetime import datetime, timedelta
    from collections import defaultdict

    period = request.args.get("period", "day")
    now = datetime.now()

    if period == "day":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        period_label = "Today"
    elif period == "week":
        cutoff = now - timedelta(days=7)
        period_label = "Last 7 Days"
    elif period == "month":
        cutoff = now - timedelta(days=30)
        period_label = "Last 30 Days"
    else:
        cutoff = datetime.fromtimestamp(0)
        period_label = "All Time"

    cfg = load_config()
    rdir = Path(cfg["recordings_dir"])

    # Aggregate from sidecars
    species_counts = defaultdict(lambda: {"count": 0, "conf_sum": 0.0, "scientific": "", "icon": "🐦"})
    daily_species = defaultdict(lambda: defaultdict(lambda: {"count": 0, "icon": "🐦"}))
    total_recordings = 0

    for sidecar in rdir.rglob("*.json"):
        try:
            d = json.loads(sidecar.read_text())
        except Exception:
            continue
        if d.get("status") != "done":
            continue
        birds = d.get("birds", [])
        if not birds:
            continue
        # Use wav mtime for date
        wav = sidecar.with_suffix(".wav")
        try:
            mtime = datetime.fromtimestamp(wav.stat().st_mtime)
        except Exception:
            continue
        if mtime < cutoff:
            continue
        total_recordings += 1
        day_key = mtime.strftime("%Y-%m-%d")
        for b in birds:
            name = b["label"]
            conf = b.get("confidence", 0)
            icon = b.get("icon", "🐦")
            sci = b.get("scientific", "")
            species_counts[name]["count"] += 1
            species_counts[name]["conf_sum"] += conf
            species_counts[name]["icon"] = icon
            if sci:
                species_counts[name]["scientific"] = sci
            daily_species[day_key][name]["count"] += 1
            daily_species[day_key][name]["icon"] = icon

    # Build leaderboard
    sorted_species = sorted(species_counts.items(), key=lambda x: x[1]["count"], reverse=True)
    max_count = sorted_species[0][1]["count"] if sorted_species else 1
    top_species = []
    for i, (name, data) in enumerate(sorted_species[:15], 1):
        top_species.append((i, {
            "label": name,
            "icon": data["icon"],
            "scientific": data["scientific"],
            "count": data["count"],
            "avg_conf": round(data["conf_sum"] / data["count"] * 100) if data["count"] else 0,
            "pct": round(data["count"] / max_count * 100),
        }))

    # Daily rows (sorted newest first)
    daily_rows = []
    for day_key in sorted(daily_species.keys(), reverse=True):
        sp_day = daily_species[day_key]
        sp_list = sorted(sp_day.items(), key=lambda x: x[1]["count"], reverse=True)
        daily_rows.append({
            "date": day_key,
            "total": sum(v["count"] for v in sp_day.values()),
            "species": [{"label": k, "icon": v["icon"], "count": v["count"]} for k, v in sp_list],
        })

    # Guessing game: top 5 species with today count + 7-day avg
    today_key = now.strftime("%Y-%m-%d")
    week_cutoff = now - timedelta(days=7)
    week_species = defaultdict(lambda: {"count": 0, "icon": "🐦"})
    today_species = defaultdict(int)

    for sidecar in rdir.rglob("*.json"):
        try:
            d = json.loads(sidecar.read_text())
        except Exception:
            continue
        if d.get("status") != "done":
            continue
        wav = sidecar.with_suffix(".wav")
        try:
            mtime = datetime.fromtimestamp(wav.stat().st_mtime)
        except Exception:
            continue
        for b in d.get("birds", []):
            name = b["label"]
            if mtime >= week_cutoff:
                week_species[name]["count"] += 1
                week_species[name]["icon"] = b.get("icon", "🐦")
            if mtime.strftime("%Y-%m-%d") == today_key:
                today_species[name] += 1

    top_for_guess = sorted(week_species.items(), key=lambda x: x[1]["count"], reverse=True)[:6]
    week_days = max(1, (now - week_cutoff).days)
    guess_species = [{
        "label": name,
        "icon": data["icon"],
        "today": today_species.get(name, 0),
        "week_avg": round(data["count"] / week_days, 1),
    } for name, data in top_for_guess if today_species.get(name, 0) > 0 or data["count"] > 0]

    total_detections = sum(d["count"] for d in species_counts.values())
    species_count = len(species_counts)
    days_active = len(daily_species)

    # Hourly heatmap
    hourly = [0] * 24
    for sidecar in rdir.rglob("*.json"):
        try:
            d = json.loads(sidecar.read_text())
            if d.get("status") != "done" or not d.get("birds"): continue
            wav = sidecar.with_suffix(".wav")
            mtime = datetime.fromtimestamp(wav.stat().st_mtime)
            if mtime < cutoff: continue
            hourly[mtime.hour] += len(d["birds"])
        except Exception:
            pass
    max_h = max(hourly) or 1
    hourly_data = [{"hour": h, "count": hourly[h],
                    "hour_label": f"{h%12 or 12}{'am' if h<12 else 'pm'}",
                    "alpha": round(0.15 + 0.85 * hourly[h] / max_h, 2) if hourly[h] else 0}
                   for h in range(24)]

    return render_template_string(STATS_TEMPLATE,
        period=period,
        period_label=period_label,
        top_species=top_species,
        daily_rows=daily_rows,
        guess_species=guess_species,
        hourly_data=hourly_data,
        total_detections=total_detections,
        total_recordings=total_recordings,
        species_count=species_count,
        days_active=days_active,
    )


if __name__ == "__main__":
    cfg = load_config()
    port = cfg.get("web_port", 8765)
    bind_ip = get_tailscale_ip()
    if not bind_ip:
        print("[mimir-web] Tailscale not up yet, retrying...")
        import time
        for _ in range(30):
            time.sleep(5)
            bind_ip = get_tailscale_ip()
            if bind_ip:
                break
    if not bind_ip:
        print("[mimir-web] ERROR: could not get Tailscale IP, binding to localhost only")
        bind_ip = "127.0.0.1"
    print(f"[mimir-web] binding to {bind_ip}:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)

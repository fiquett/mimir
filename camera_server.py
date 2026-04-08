#!/usr/bin/env python3
"""
uplink camera server — triggered by mimir when BirdNET detects a bird.
Captures a 10s video clip (pre-roll 2s + post-roll 8s) from the USB cam.
Serves clips over HTTP for mimir web UI.
"""

import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, abort, Response

MEDIA_DIR = Path("/home/pi/mimir-photos")
MEDIA_DIR.mkdir(exist_ok=True)
PORT = 8766

# Pre-roll buffer: continuously record into a ring file so we can
# grab the last N seconds when triggered.
PREROLL_SECS = 3
CLIP_SECS = 12        # total clip length (pre-roll + post-roll)
RESOLUTION = "1280x720"
FPS = 15

app = Flask(__name__)
_capture_lock = threading.Lock()


def _find_device():
    """Find first real USB video device."""
    for d in sorted(Path("/dev").glob("video*")):
        try:
            # Check it's a capture device (not encoder/meta)
            result = subprocess.run(
                ["v4l2-ctl", "--device", str(d), "--list-formats"],
                capture_output=True, timeout=2
            )
            if b"MJPG" in result.stdout or b"YUYV" in result.stdout:
                return str(d)
        except Exception:
            pass
    # Fallback: just use video0 if it exists
    if Path("/dev/video0").exists():
        return "/dev/video0"
    return None


def capture_clip(label="bird", confidence=0.0, recording_path=""):
    """Capture a video clip. Returns filename or None."""
    if not _capture_lock.acquire(blocking=False):
        print("[camera] capture already in progress, skipping")
        return None

    try:
        device = _find_device()
        if not device:
            print("[camera] no capture device found")
            return None

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        fname = f"{ts}_{label.replace(' ', '_')}_{int(confidence*100)}.mp4"
        fpath = MEDIA_DIR / fname

        # ffmpeg: read from webcam for CLIP_SECS seconds
        # -ss 0 skips nothing; we use a ring buffer approach via segment muxer
        # Simplest reliable approach: just record CLIP_SECS from NOW
        # (mimir triggers ~1-2s into the event due to BirdNET analysis time,
        #  so clip naturally contains the event + aftermath)
        cmd = [
            "ffmpeg", "-y",
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-framerate", str(FPS),
            "-video_size", RESOLUTION,
            "-i", device,
            "-t", str(CLIP_SECS),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-movflags", "+faststart",
            str(fpath),
        ]
        print(f"[camera] recording {CLIP_SECS}s clip → {fname}")
        result = subprocess.run(cmd, timeout=CLIP_SECS + 10,
                                capture_output=True)
        if result.returncode != 0:
            # Try YUYV if MJPEG failed
            cmd[5] = "yuyv422"
            result = subprocess.run(cmd, timeout=CLIP_SECS + 10,
                                    capture_output=True)

        if fpath.exists() and fpath.stat().st_size > 10000:
            # Also grab a thumbnail still from frame 1
            thumb = str(fpath).replace(".mp4", "_thumb.jpg")
            subprocess.run([
                "ffmpeg", "-y", "-i", str(fpath),
                "-vframes", "1", "-q:v", "3", thumb,
            ], capture_output=True, timeout=5)

            meta = {
                "video": fname,
                "thumb": fname.replace(".mp4", "_thumb.jpg"),
                "label": label,
                "confidence": confidence,
                "recording": recording_path,
                "ts": time.time(),
                "duration": CLIP_SECS,
            }
            sidecar = MEDIA_DIR / fname.replace(".mp4", ".json")
            sidecar.write_text(json.dumps(meta, indent=2))
            print(f"[camera] saved {fname}")
            return fname
        else:
            print(f"[camera] clip too small or missing: {fpath}")
            return None

    except Exception as e:
        print(f"[camera] capture failed: {e}")
        return None
    finally:
        _capture_lock.release()


@app.route("/capture", methods=["POST"])
def api_capture():
    data = request.get_json(silent=True) or {}
    label = data.get("label", "bird")
    confidence = float(data.get("confidence", 0))
    recording = data.get("recording", "")

    def _bg():
        fname = capture_clip(label, confidence, recording)
        if fname:
            # Write photo field into mimir sidecar for web UI
            try:
                rec_path = Path(recording)
                sidecar = rec_path.with_suffix(".json")
                if sidecar.exists():
                    d = json.loads(sidecar.read_text())
                    d["photo"] = fname.replace(".mp4", "_thumb.jpg")
                    d["video"] = fname
                    sidecar.write_text(json.dumps(d, indent=2))
            except Exception:
                pass

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"status": "capturing", "label": label})


@app.route("/photos/<path:fname>")
def serve_photo(fname):
    return send_from_directory(MEDIA_DIR, fname)


@app.route("/clips")
def list_clips():
    clips = sorted(MEDIA_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
    return jsonify([p.name for p in clips[:50]])


@app.route("/clips_meta")
def clips_meta():
    """Return [{fname, thumb, ts, label}] for recent clips — used by mimir to link by timestamp."""
    clips = sorted(MEDIA_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    result = []
    for c in clips[:100]:
        try:
            meta = json.loads(c.read_text())
            if "video" not in meta:
                continue
            result.append({
                "fname": meta["video"],
                "thumb": meta.get("thumb", ""),
                "ts": meta.get("ts", c.stat().st_mtime),
                "label": meta.get("label", ""),
            })
        except Exception:
            pass
    return jsonify(result)


@app.route("/label_clip/<path:fname>", methods=["POST"])
def label_clip(fname):
    """Update a clip's label after BirdNET identifies the species."""
    data = request.get_json(silent=True) or {}
    sidecar = MEDIA_DIR / fname.replace(".mp4", ".json")
    if sidecar.exists():
        try:
            meta = json.loads(sidecar.read_text())
            meta["label"] = data.get("label", meta.get("label", ""))
            meta["confidence"] = data.get("confidence", meta.get("confidence", 0))
            sidecar.write_text(json.dumps(meta, indent=2))
        except Exception:
            pass
    return jsonify({"ok": True})



@app.route("/live")
def live_stream():
    """MJPEG live stream from the webcam."""
    device = _find_device()
    if not device:
        abort(503, "No camera device found")
    def generate():
        cmd = [
            "ffmpeg", "-y",
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-framerate", "10",
            "-video_size", "640x480",
            "-i", device,
            "-f", "mjpeg",
            "-q:v", "5",
            "-r", "5",
            "pipe:1",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while True:
                # Read JPEG frames from pipe
                buf = b""
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        return
                    buf += chunk
                    # Find JPEG boundaries
                    start = buf.find(b"\xff\xd8")
                    end = buf.find(b"\xff\xd9", start + 2)
                    if start >= 0 and end >= 0:
                        frame = buf[start:end+2]
                        buf = buf[end+2:]
                        yield (b"--frame\r\n"
                               b"Content-Type: image/jpeg\r\n\r\n" +
                               frame + b"\r\n")
        finally:
            proc.kill()
            proc.wait()
    return Response(generate(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/preview")
def preview_page():
    """Live camera preview page with controls."""
    clips = sorted(MEDIA_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)[:6]
    clip_html = ""
    for c in clips:
        thumb = c.with_name(c.stem + "_thumb.jpg")
        if thumb.exists():
            clip_html += f'''<div style="display:inline-block;margin:4px">
              <img src="/photos/{thumb.name}" style="width:160px;border-radius:4px;border:1px solid #30363d;cursor:pointer"
                   onclick="window.open(\'/photos/{c.name}\')" title="{c.name}">
            </div>'''
    return f'''<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>uplink camera</title>
<style>
body{{font-family:"SF Mono",monospace;background:#0d1117;color:#c9d1d9;padding:16px;max-width:800px;margin:0 auto}}
h1{{color:#58a6ff;font-size:1.1rem;margin-bottom:12px}}
img.live{{width:100%;max-width:640px;border-radius:8px;border:1px solid #30363d}}
.controls{{margin:12px 0;display:flex;gap:8px;flex-wrap:wrap}}
button{{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:6px 14px;border-radius:6px;cursor:pointer;font-family:inherit;font-size:0.85rem}}
button:hover{{background:#30363d}}
.section{{font-size:0.75rem;text-transform:uppercase;color:#8b949e;letter-spacing:1px;margin:16px 0 8px}}
</style></head><body>
<h1>uplink camera preview</h1>
<img class="live" src="/live" alt="live feed">
<div class="controls">
  <button onclick="fetch(\'/capture\',{{method:\'POST\',headers:{{\"Content-Type\":\"application/json\"}},body:\'{{\"label\":\"manual\",\"confidence\":1.0}}\'}}); this.textContent=\'capturing...\'; setTimeout(()=>location.reload(),15000)">Capture Clip</button>
  <button onclick="document.querySelector(\'.live\').src=\'/live?\'+Date.now()">Refresh Stream</button>
</div>
<div class="section">Camera Controls</div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;max-width:500px">
  <label style="font-size:0.8rem;color:#8b949e">Brightness<br>
    <input type="range" min="0" max="255" value="128" id="c-brightness"
      oninput="setCtrl(\'brightness\',this.value)" style="width:100%"></label>
  <label style="font-size:0.8rem;color:#8b949e">Gain<br>
    <input type="range" min="0" max="255" value="16" id="c-gain"
      oninput="setCtrl(\'gain\',this.value)" style="width:100%"></label>
  <label style="font-size:0.8rem;color:#8b949e">Contrast<br>
    <input type="range" min="0" max="255" value="32" id="c-contrast"
      oninput="setCtrl(\'contrast\',this.value)" style="width:100%"></label>
  <label style="font-size:0.8rem;color:#8b949e">Saturation<br>
    <input type="range" min="0" max="255" value="32" id="c-saturation"
      oninput="setCtrl(\'saturation\',this.value)" style="width:100%"></label>
  <label style="font-size:0.8rem;color:#8b949e">Sharpness<br>
    <input type="range" min="0" max="255" value="72" id="c-sharpness"
      oninput="setCtrl(\'sharpness\',this.value)" style="width:100%"></label>
  <label style="font-size:0.8rem;color:#8b949e">Backlight comp<br>
    <input type="range" min="0" max="1" value="0" id="c-backlight_compensation"
      oninput="setCtrl(\'backlight_compensation\',this.value)" style="width:100%"></label>
  <label style="font-size:0.8rem;color:#8b949e">Pan (-36000 to 36000)<br>
    <input type="range" min="-36000" max="36000" step="3600" value="0" id="c-pan_absolute"
      oninput="setCtrl(\'pan_absolute\',this.value)" style="width:100%"></label>
  <label style="font-size:0.8rem;color:#8b949e">Tilt (-36000 to 36000)<br>
    <input type="range" min="-36000" max="36000" step="3600" value="0" id="c-tilt_absolute"
      oninput="setCtrl(\'tilt_absolute\',this.value)" style="width:100%"></label>
</div>
<script>
function setCtrl(name,val){{
  fetch(\'/set_control\',{{method:\'POST\',headers:{{\"Content-Type\":\"application/json\"}},body:JSON.stringify({{name,value:parseInt(val)}})}});
}}
fetch(\'/controls\').then(r=>r.json()).then(d=>{{
  for(const[k,v]of Object.entries(d)){{
    const el=document.getElementById(\'c-\'+k);
    if(el)el.value=v;
  }}
}});
</script>
<div class="section">Recent Clips</div>
{clip_html}
</body></html>'''



@app.route("/controls")
def get_controls():
    """Return current camera controls."""
    ctrls = {}
    for name in ["brightness","contrast","saturation","gain","sharpness",
                  "backlight_compensation","zoom_absolute","pan_absolute","tilt_absolute"]:
        try:
            r = subprocess.run(["v4l2-ctl","-d",_find_device(),"--get-ctrl",name],
                               capture_output=True, timeout=2)
            val = r.stdout.decode().strip().split(":")[-1].strip()
            ctrls[name] = int(val)
        except: pass
    return jsonify(ctrls)


@app.route("/set_control", methods=["POST"])
def set_control():
    """Set a camera control. Body: {name: str, value: int}"""
    d = request.get_json(silent=True) or {}
    name = d.get("name","")
    value = d.get("value",0)
    allowed = {"brightness","contrast","saturation","gain","sharpness",
               "backlight_compensation","zoom_absolute","pan_absolute","tilt_absolute"}
    if name not in allowed:
        return jsonify({"error": "invalid control"}), 400
    subprocess.run(["v4l2-ctl","-d",_find_device(),"--set-ctrl",f"{name}={value}"],
                   timeout=2)
    return jsonify({"ok": True, name: value})


@app.route("/health")
def health():
    device = _find_device()
    clips = len(list(MEDIA_DIR.glob("*.mp4")))
    return jsonify({"status": "ok", "device": device, "clips": clips})


if __name__ == "__main__":
    print(f"[camera] serving on :{PORT}, media → {MEDIA_DIR}")
    app.run(host="0.0.0.0", port=PORT)

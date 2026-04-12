#!/usr/bin/env python3
"""
mimir camera — RTSP clip capture + motion detection for bird monitoring.

Two modes:
1. Audio-triggered: called by analysis.py when BirdNET detects a bird
2. Motion-triggered: polls RTSP stream for movement in frame, captures clips independently

Clips are stored alongside audio recordings with cross-reference by timestamp.
"""

import json
import subprocess
import threading
import time
import os
import urllib.request
import ssl
from datetime import datetime
from pathlib import Path


def _send_ntfy(title, body, cfg):
    """Send text-only push notification via ntfy.sh. No images ever leave the network."""
    topic = cfg.get("ntfy_topic", "").strip()
    if not topic:
        return
    def _send():
        try:
            base_url = cfg.get("tailscale_url") or cfg.get("local_url") or "http://localhost:8765"
            payload = json.dumps({
                "topic": topic, "title": title, "message": body,
                "priority": 3, "tags": ["camera"],
                "click": f"{base_url}/camera_feed",
                "actions": [{"action": "view", "label": "View", "url": f"{base_url}/camera_feed"}],
            }).encode()
            req = urllib.request.Request(
                "https://ntfy.sh", data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10)
            print(f"[camera] ntfy sent: {title}")
        except Exception as e:
            print(f"[camera] ntfy failed: {e}")
    threading.Thread(target=_send, daemon=True).start()

# ── Config ──────────────────────────────────────────────────────

def load_config():
    for p in [Path("/home/pi/mimir/config.json"), Path("config.json")]:
        if p.exists():
            return json.loads(p.read_text())
    return {}


CLIPS_DIR = Path("/mnt/usb/camera")
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

_capture_lock = threading.Lock()

# Shared state for cross-detection verification
# Updated by motion detector, read by audio-triggered captures
_motion_state = {"last_motion": 0, "last_motion_pct": 0, "last_ai_animal": 0}


def get_motion_state():
    """Return current motion detector state."""
    return dict(_motion_state)


def identify_species_visual(thumb_path, cfg=None):
    """Send thumbnail to Claude vision API for species identification.
    Returns {species, confidence_text, description} or None."""
    cfg = cfg or load_config()
    api_key = cfg.get("anthropic_api_key", "")
    if not api_key or not thumb_path or not Path(thumb_path).exists():
        return None

    def _identify():
        try:
            import base64
            img_data = base64.b64encode(Path(thumb_path).read_bytes()).decode()
            payload = json.dumps({
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_data}},
                        {"type": "text", "text": "This is from an outdoor bird monitoring camera on a balcony in Seattle. Is there a bird or animal visible? If so, identify the species as specifically as possible. Respond in JSON only: {\"animal_present\": bool, \"species\": \"common name\", \"confidence\": \"high/medium/low\", \"description\": \"brief description of what you see\"}. If no animal is visible, set animal_present to false."}
                    ]
                }]
            }).encode()

            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read())

            text = resp.get("content", [{}])[0].get("text", "")
            # Extract JSON from response
            import re
            match = re.search(r'\{[^}]+\}', text, re.DOTALL)
            if match:
                result = json.loads(match.group())
                if result.get("animal_present"):
                    print(f"[camera] vision ID: {result.get('species')} ({result.get('confidence')})")

                    # Update sidecar with vision ID
                    sidecar = Path(thumb_path).with_name(
                        Path(thumb_path).name.replace("_thumb.jpg", ".json"))
                    if sidecar.exists():
                        meta = json.loads(sidecar.read_text())
                    else:
                        meta = {}
                    meta["vision_id"] = result
                    sidecar.write_text(json.dumps(meta, indent=2))

                    # Send ntfy if it's a bird
                    species = result.get("species", "unknown")
                    conf = result.get("confidence", "")
                    desc = result.get("description", "")
                    _send_ntfy(
                        f"👁 {species} (visual ID)",
                        f"Confidence: {conf}\n{desc}",
                        cfg,
                    )
                    return result
                else:
                    print(f"[camera] vision ID: no animal visible")
            return None
        except Exception as e:
            print(f"[camera] vision ID error: {e}")
            return None

    return _identify()


def get_rtsp_url(cfg=None, sub=False):
    cfg = cfg or load_config()
    url = cfg.get("rtsp_url", "")
    if sub and url:
        return url.replace("_01_main", "_01_sub")
    return url


# ── Clip capture ────────────────────────────────────────────────

def capture_clip(label="bird", confidence=0.0, duration=12, cfg=None):
    """Capture a video clip from RTSP stream. Returns (mp4_path, thumb_path) or (None, None)."""
    if not _capture_lock.acquire(blocking=False):
        print("[camera] capture already in progress, skipping")
        return None, None

    try:
        cfg = cfg or load_config()
        rtsp_url = get_rtsp_url(cfg)
        if not rtsp_url:
            print("[camera] no rtsp_url configured")
            return None, None

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        day_dir = CLIPS_DIR / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(exist_ok=True)

        safe_label = label.replace(" ", "_").replace("'", "").replace("(", "").replace(")", "")
        fname = f"{ts}_{safe_label}_{int(confidence*100)}.mp4"
        fpath = day_dir / fname

        cmd = [
            "ffmpeg", "-y",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-t", str(duration),
            "-c:v", "copy",  # source is H.264, no transcode needed (zero CPU)
            "-an",
            "-movflags", "+faststart",
            str(fpath),
        ]
        print(f"[camera] recording {duration}s clip → {fname}")
        result = subprocess.run(cmd, timeout=duration + 15, capture_output=True)

        if fpath.exists() and fpath.stat().st_size > 10000:
            # Extract thumbnail from SUB stream (640x360, low CPU) instead of decoding 4K
            thumb_path = fpath.with_name(fpath.stem + "_thumb.jpg")
            sub_url = rtsp_url.replace("_01_main", "_01_sub")
            subprocess.run([
                "ffmpeg", "-y",
                "-rtsp_transport", "tcp",
                "-i", sub_url,
                "-vframes", "1", "-q:v", "3", "-update", "1",
                str(thumb_path),
            ], timeout=8, capture_output=True)

            crop_path = fpath.with_name(fpath.stem + "_crop.jpg")  # placeholder, no longer generated

            # Write sidecar with verification
            now = time.time()
            motion_age = now - _motion_state["last_motion"] if _motion_state["last_motion"] else 999
            ai_age = now - _motion_state["last_ai_animal"] if _motion_state["last_ai_animal"] else 999
            verified_visual = motion_age < 30 or ai_age < 60
            sidecar = fpath.with_suffix(".json")
            sidecar.write_text(json.dumps({
                "ts": now,
                "label": label,
                "confidence": confidence,
                "source": "rtsp",
                "thumb": thumb_path.name if thumb_path.exists() else None,
                "verified_visual": verified_visual,
                "motion_age_s": round(motion_age, 1) if motion_age < 999 else None,
                "ai_age_s": round(ai_age, 1) if ai_age < 999 else None,
            }, indent=2))

            print(f"[camera] captured {fname} ({fpath.stat().st_size // 1024}KB)")

            # Visual species ID disabled — no images leave the network
            # TODO: replace with local bird classifier (MobileNet)

            return fpath, thumb_path if thumb_path.exists() else None
        else:
            print(f"[camera] capture failed or too small")
            if fpath.exists():
                fpath.unlink()
            return None, None
    except subprocess.TimeoutExpired:
        print("[camera] capture timed out")
        return None, None
    except Exception as e:
        print(f"[camera] capture error: {e}")
        return None, None
    finally:
        _capture_lock.release()


def capture_clip_async(label="bird", confidence=0.0, duration=12, cfg=None, callback=None):
    """Fire-and-forget clip capture in a background thread."""
    def _run():
        mp4, thumb = capture_clip(label, confidence, duration, cfg)
        if callback and mp4:
            callback(mp4, thumb)
    threading.Thread(target=_run, daemon=True).start()


def grab_snapshot(cfg=None):
    """Grab a single JPEG frame from RTSP. Returns path or None."""
    cfg = cfg or load_config()
    rtsp_url = get_rtsp_url(cfg)
    if not rtsp_url:
        return None

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    day_dir = CLIPS_DIR / datetime.now().strftime("%Y-%m-%d")
    day_dir.mkdir(exist_ok=True)
    fpath = day_dir / f"{ts}_snapshot.jpg"

    try:
        result = subprocess.run([
            "ffmpeg", "-y",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-frames:v", "1", "-q:v", "3",
            "-update", "1",
            str(fpath),
        ], timeout=10, capture_output=True)
        if fpath.exists() and fpath.stat().st_size > 5000:
            return fpath
    except Exception as e:
        print(f"[camera] snapshot error: {e}")
    return None


# ── Motion detection ────────────────────────────────────────────

class MotionDetector:
    """Simple frame-diff motion detector on RTSP stream."""

    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self.rtsp_url = get_rtsp_url(self.cfg, sub=True)  # sub-stream for motion (H.264, lower res)
        self.running = False
        self._thread = None
        # Motion sensitivity: percentage of pixels that must change
        self.min_area_pct = self.cfg.get("motion_min_area_pct", 1.5)
        # Cooldown between motion captures (seconds)
        self.cooldown = self.cfg.get("motion_cooldown", 15)
        self._last_capture = 0

    def start(self):
        if not self.rtsp_url:
            print("[camera] no rtsp_url, motion detection disabled")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print("[camera] motion detector started")

    def stop(self):
        self.running = False

    def _run(self):
        import cv2
        import numpy as np

        while self.running:
            cap = None
            try:
                cap = cv2.VideoCapture(self.rtsp_url)
                if not cap.isOpened():
                    print("[camera] can't open RTSP for motion detection, retrying in 30s")
                    time.sleep(30)
                    continue

                prev_gray = None
                while self.running:
                    ret, frame = cap.read()
                    if not ret:
                        print("[camera] lost RTSP feed, reconnecting...")
                        break

                    # Downscale for performance
                    small = cv2.resize(frame, (320, 180))
                    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                    gray = cv2.GaussianBlur(gray, (21, 21), 0)

                    if prev_gray is None:
                        prev_gray = gray
                        time.sleep(0.5)
                        continue

                    # Frame difference
                    delta = cv2.absdiff(prev_gray, gray)
                    thresh = cv2.threshold(delta, 30, 255, cv2.THRESH_BINARY)[1]
                    thresh = cv2.dilate(thresh, None, iterations=2)

                    # Percentage of changed pixels
                    changed_pct = (thresh.sum() / 255) / (320 * 180) * 100

                    if changed_pct > self.min_area_pct:
                        now = time.time()
                        # Always update shared motion state for audio verification
                        _motion_state["last_motion"] = now
                        _motion_state["last_motion_pct"] = changed_pct
                        if now - self._last_capture > self.cooldown:
                            self._last_capture = now
                            print(f"[camera] motion detected ({changed_pct:.1f}% changed)")
                            capture_clip_async(
                                label="motion",
                                confidence=min(1.0, changed_pct / 10),
                                duration=12,
                                cfg=self.cfg,
                            )

                    prev_gray = gray
                    # Process ~2 frames/sec to keep CPU low
                    time.sleep(0.5)

            except Exception as e:
                print(f"[camera] motion detector error: {e}")
                time.sleep(10)
            finally:
                if cap:
                    cap.release()


# ── Reolink AI polling (animal/people detection) ────────────────

class ReolinkAlarmPoller:
    """Poll Reolink camera's built-in AI detection for animal alerts."""

    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self.camera_ip = self.cfg.get("camera_ip", "")
        self.camera_user = self.cfg.get("camera_user", "admin")
        self.camera_pass = self.cfg.get("camera_pass", "")
        self.running = False
        self._thread = None
        self._last_alarm = 0
        self.cooldown = self.cfg.get("alarm_cooldown", 30)
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    def start(self):
        if not self.camera_ip or not self.camera_pass:
            print("[camera] no camera_ip/camera_pass, Reolink alarm polling disabled")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[camera] Reolink alarm poller started for {self.camera_ip}")

    def stop(self):
        self.running = False

    def _run(self):
        while self.running:
            try:
                url = f"https://{self.camera_ip}/api.cgi?cmd=GetAiState&user={self.camera_user}&password={self.camera_pass}"
                data = json.dumps([{"cmd": "GetAiState", "action": 0, "param": {"channel": 0}}]).encode()
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5, context=self._ctx) as r:
                    result = json.loads(r.read())

                ai_state = result[0].get("value", {}) if result else {}
                dog_cat = ai_state.get("dog_cat", {}).get("alarm_state", 0)
                people = ai_state.get("people", {}).get("alarm_state", 0)

                now = time.time()
                if dog_cat and now - self._last_alarm > self.cooldown:
                    self._last_alarm = now
                    _motion_state["last_ai_animal"] = now
                    print("[camera] Reolink AI: animal detected!")
                    _send_ntfy("🐦 Animal on balcony", "Reolink camera detected an animal — check camera feed", self.cfg)
                    capture_clip_async(
                        label="animal (camera AI)",
                        confidence=0.8,
                        duration=12,
                        cfg=self.cfg,
                    )
                elif people and now - self._last_alarm > self.cooldown:
                    self._last_alarm = now
                    print("[camera] Reolink AI: person detected")
                    capture_clip_async(
                        label="person (camera AI)",
                        confidence=0.8,
                        duration=10,
                        cfg=self.cfg,
                    )
            except Exception as e:
                if "Connection refused" not in str(e):
                    print(f"[camera] alarm poll error: {e}")

            time.sleep(2)  # Poll every 2 seconds


# ── Main: run as standalone service ─────────────────────────────

if __name__ == "__main__":
    cfg = load_config()
    print(f"[camera] RTSP: {get_rtsp_url(cfg)}")
    print(f"[camera] clips dir: {CLIPS_DIR}")

    # Start motion detector
    motion = MotionDetector(cfg)
    motion.start()

    # Start Reolink AI polling
    alarm = ReolinkAlarmPoller(cfg)
    alarm.start()

    # Keep alive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        motion.stop()
        alarm.stop()

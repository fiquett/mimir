#!/usr/bin/env python3
"""
mimir analysis — background audio classifier + speech transcriber.
Writes a sidecar .json next to each .wav with tags and transcript.
"""

import json
import queue
import threading
import wave
import numpy as np
from pathlib import Path
try:
    import urllib.request
except ImportError:
    pass

# ── Species image cache ─────────────────────────────────────────

_IMAGE_CACHE_PATH = Path("/home/pi/mimir/species_images.json")
_image_cache = None
_image_cache_lock = threading.Lock()


def _load_image_cache():
    global _image_cache
    if _image_cache is None:
        try:
            _image_cache = json.loads(_IMAGE_CACHE_PATH.read_text()) if _IMAGE_CACHE_PATH.exists() else {}
        except Exception:
            _image_cache = {}
    return _image_cache


def _save_image_cache():
    try:
        _IMAGE_CACHE_PATH.write_text(json.dumps(_image_cache, indent=2))
    except Exception:
        pass


def _notify_birds(detections, wav_path, cfg):
    """Send ntfy.sh push notification for target species detections."""
    topic = cfg.get("ntfy_topic", "").strip()
    if not topic:
        return
    species_filter = [s.strip().lower() for s in cfg.get("ntfy_species", "").split(",") if s.strip()]
    matches = [d for d in detections
               if not species_filter or d["label"].lower() in species_filter]
    if not matches:
        return
    def _send():
        try:
            top = matches[0]
            others = [d["label"] for d in matches[1:3]]
            title = f"{top.get('icon','🐦')} {top['label'].title()} detected"
            body = f"{int(top['confidence']*100)}% confidence"
            if others:
                body += f" · also: {', '.join(others)}"
            base_url = cfg.get("tailscale_url") or cfg.get("local_url") or "http://localhost:8765"
            clip_url = f"{base_url}/clip/{wav_path.name}"
            body += f"\n{wav_path.name}"
            payload = json.dumps({"topic": topic, "title": title, "message": body,
                                  "priority": 3, "tags": ["bird"],
                                  "click": clip_url,
                                  "actions": [{"action": "view", "label": "Listen", "url": clip_url}]
                                  }).encode()
            req = urllib.request.Request(
                "https://ntfy.sh",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=8)
            print(f"[analysis] ntfy sent: {title}")
        except Exception as e:
            print(f"[analysis] ntfy failed: {e}")
    threading.Thread(target=_send, daemon=True).start()


def fetch_species_image(common_name):
    """Return {url, description} for a bird species, cached locally. None on failure."""
    with _image_cache_lock:
        cache = _load_image_cache()
        if common_name in cache:
            return cache[common_name]
        try:
            # Build Wikipedia slug: "violet-green swallow" → "Violet-green_swallow"
            # Capitalize first letter of each space-separated word, preserve hyphens
            parts = common_name.split()
            slug = "_".join(p[0].upper() + p[1:] if p else p for p in parts)
            req = urllib.request.Request(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}",
                headers={"User-Agent": "mimir-birdcam/1.0 (pi bird monitor)"}
            )
            with urllib.request.urlopen(req, timeout=6) as r:
                d = json.loads(r.read())
            thumb = d.get("thumbnail", {}).get("source")
            if not thumb:
                cache[common_name] = None
                _save_image_cache()
                return None
            # Get larger version — Wikipedia thumbnails have /NNNpx- in URL, bump to 400px
            import re
            thumb = re.sub(r'/\d+px-', '/400px-', thumb)
            result = {"url": thumb, "desc": d.get("description", "")}
            cache[common_name] = result
            _save_image_cache()
            print(f"[analysis] fetched image for {common_name}")
            return result
        except Exception as e:
            print(f"[analysis] image fetch failed for {common_name}: {e}")
            cache[common_name] = None
            _save_image_cache()
            return None

# ── Spectral sound classifier ───────────────────────────────────

SOUND_ICONS = {
    "speech":       "🗣",
    "bird":         "🐦",
    "aircraft":     "✈",
    "helicopter":   "🚁",
    "vehicle":      "🚗",
    "rain":         "🌧",
    "wind":         "💨",
    "music":        "🎵",
}


def _band_energy(spectrum, freqs, low, high):
    mask = (freqs >= low) & (freqs < high)
    total = spectrum.sum()
    return float(spectrum[mask].sum() / total) if total > 0 else 0.0


def _am_depth(samples, sr, low_hz=8, high_hz=50):
    """Measure amplitude modulation depth in the given Hz range (helicopter blade slap)."""
    # Envelope via abs + low-pass
    envelope = np.abs(samples)
    # Smooth with a 5ms window
    win = max(1, int(sr * 0.005))
    kernel = np.ones(win) / win
    envelope = np.convolve(envelope, kernel, mode='same')
    # FFT of envelope
    n = len(envelope)
    freqs_env = np.fft.rfftfreq(n, 1 / sr)
    spectrum_env = np.abs(np.fft.rfft(envelope))
    mask = (freqs_env >= low_hz) & (freqs_env <= high_hz)
    total = spectrum_env.sum()
    return float(spectrum_env[mask].sum() / total) if total > 0 else 0.0


def classify_sounds(wav_path):
    """Return list of {label, icon, confidence} dicts for detected sounds."""
    try:
        with wave.open(str(wav_path)) as wf:
            sr = wf.getframerate()
            n_ch = wf.getnchannels()
            raw = wf.readframes(wf.getnframes())

        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if n_ch > 1:
            samples = samples[::n_ch]

        if len(samples) < sr:  # < 1 second, skip
            return []

        # Average spectrum over multiple windows
        n_fft = 4096
        n_win = min(20, len(samples) // n_fft)
        if n_win == 0:
            n_fft = len(samples)
            n_win = 1

        freqs = np.fft.rfftfreq(n_fft, 1 / sr)
        spectra = []
        step = len(samples) // n_win
        for i in range(n_win):
            chunk = samples[i * step: i * step + n_fft]
            if len(chunk) < n_fft:
                chunk = np.pad(chunk, (0, n_fft - len(chunk)))
            spectra.append(np.abs(np.fft.rfft(chunk)))
        mean_spec = np.mean(spectra, axis=0)

        # Frequency band energies
        sub      = _band_energy(mean_spec, freqs,  20,   80)   # rumble
        bass     = _band_energy(mean_spec, freqs,  80,  300)   # vehicles, thunder
        voice_lo = _band_energy(mean_spec, freqs, 300, 1000)   # voiced speech
        voice_hi = _band_energy(mean_spec, freqs, 1000, 4000)  # speech presence/sibilants
        bird_lo  = _band_energy(mean_spec, freqs, 1500, 5000)  # bird fundamentals
        bird_hi  = _band_energy(mean_spec, freqs, 5000, 10000) # bird harmonics
        rain_b   = _band_energy(mean_spec, freqs,  500, 8000)  # broadband noise

        # Spectral flatness (noise-like vs tonal)
        geo_mean = np.exp(np.mean(np.log(mean_spec + 1e-10)))
        arith_mean = np.mean(mean_spec) + 1e-10
        flatness = float(geo_mean / arith_mean)  # 0=tonal, 1=noise

        # Helicopter: strong low-frequency AM modulation at blade-rate (8–40 Hz)
        heli_am = _am_depth(samples, sr, 8, 40)

        tags = []

        # ── Aircraft ──
        aircraft_energy = sub + bass * 0.6
        if aircraft_energy > 0.25:
            # Nearly all overhead noise is fixed-wing aircraft, not helicopters.
            # Only tag as helicopter with very strong blade-slap AM modulation.
            if heli_am > 0.30:
                conf = min(1.0, heli_am * 3 + aircraft_energy)
                tags.append({"label": "helicopter", "confidence": round(conf, 2)})
            else:
                tags.append({"label": "aircraft", "confidence": round(aircraft_energy, 2)})

        # ── Ground vehicle ──
        elif bass > 0.20 and sub > 0.08 and flatness < 0.40:
            tags.append({"label": "vehicle", "confidence": round(bass, 2)})

        # ── Speech ──
        speech_score = voice_lo * 0.6 + voice_hi * 0.4
        if speech_score > 0.22 and flatness < 0.65:
            tags.append({"label": "speech", "confidence": round(speech_score, 2)})

        # ── Bird ──
        bird_score = bird_lo * 0.7 + bird_hi * 0.3
        if bird_score > 0.13 and flatness < 0.50 and aircraft_energy < 0.20:
            tags.append({"label": "bird", "confidence": round(bird_score, 2)})

        # ── Rain (broadband, spectrally flat, dominant mid-high energy) ──
        if flatness > 0.60 and rain_b > 0.45:
            tags.append({"label": "rain", "confidence": round(min(flatness, 0.99), 2)})

        # ── Wind (low-frequency broadband) ──
        elif flatness > 0.45 and sub + bass > 0.35 and speech_score < 0.20:
            tags.append({"label": "wind", "confidence": round(flatness * 0.8, 2)})

        # ── Fallback: broadband noise (unclassified) ──
        if not tags and flatness > 0.55:
            tags.append({"label": "noise", "icon": "〰", "confidence": round(flatness * 0.6, 2)})

        # Sort by confidence
        tags.sort(key=lambda t: t["confidence"], reverse=True)
        for t in tags:
            if "icon" not in t:
                t["icon"] = SOUND_ICONS.get(t["label"], "🔊")
        return tags

    except Exception as e:
        return [{"label": "error", "icon": "⚠", "confidence": 0, "detail": str(e)}]


# ── BirdNET species classifier ──────────────────────────────────

# Capitol Hill, Seattle — used for BirdNET regional filtering
MIMIR_LAT = 47.6297
MIMIR_LON = -122.3208

BIRD_SPECIES_ICONS = {
    "american crow":    "🐦‍⬛",
    "common raven":     "🪶",
    "steller's jay":    "🔵",
    "blue jay":         "🔵",
    "eurasian jackdaw": "🐦‍⬛",
    "fish crow":        "🐦‍⬛",
}

_birdnet_analyzer = None
_birdnet_lock = threading.Lock()


def _get_birdnet():
    global _birdnet_analyzer
    with _birdnet_lock:
        if _birdnet_analyzer is None:
            try:
                from birdnetlib.analyzer import Analyzer as BNAnalyzer
                print("[analysis] loading BirdNET model...")
                _birdnet_analyzer = BNAnalyzer()
                print("[analysis] BirdNET ready")
            except Exception as e:
                print(f"[analysis] BirdNET unavailable: {e}")
                _birdnet_analyzer = False
    return _birdnet_analyzer if _birdnet_analyzer else None


def _link_camera_clip(camera_url, wav_path, detection):
    """Find the camera clip closest in time to this recording and link it in the sidecar."""
    def _link():
        try:
            req = urllib.request.Request(
                f"{camera_url.rstrip('/')}/clips_meta")
            with urllib.request.urlopen(req, timeout=5) as r:
                clips = json.loads(r.read())  # [{fname, ts}, ...]
            if not clips:
                return
            # Find clip whose timestamp is within 30s before the wav file's mtime
            wav_ts = wav_path.stat().st_mtime
            best = None
            best_delta = float("inf")
            for c in clips:
                delta = wav_ts - c["ts"]
                if -5 < delta < 60 and delta < best_delta:
                    best_delta = delta
                    best = c
            if best:
                sidecar = wav_path.with_suffix(".json")
                if sidecar.exists():
                    data = json.loads(sidecar.read_text())
                    data["photo"] = best["thumb"]
                    data["video"] = best["fname"]
                    data["bird_label"] = detection["label"]
                    # Also push species label back to camera sidecar
                    try:
                        upd = json.dumps({"label": detection["label"],
                                          "confidence": detection["confidence"]}).encode()
                        req2 = urllib.request.Request(
                            f"{camera_url.rstrip('/')}/label_clip/{best['fname']}",
                            data=upd,
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        urllib.request.urlopen(req2, timeout=5)
                    except Exception:
                        pass
                    sidecar.write_text(json.dumps(data, indent=2))
                    print(f"[analysis] linked clip {best['fname']} → {wav_path.name}")
        except Exception as e:
            print(f"[analysis] clip link failed: {e}")
    threading.Thread(target=_link, daemon=True).start()


def _trigger_camera(camera_url, detection, recording_path):
    """Fire-and-forget POST to uplink camera server; writes result back to sidecar."""
    def _post():
        try:
            payload = json.dumps({
                "label": detection["label"],
                "confidence": detection["confidence"],
                "recording": recording_path,
            }).encode()
            req = urllib.request.Request(
                f"{camera_url.rstrip('/')}/capture",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read())
            fname = resp.get("video") or resp.get("photo")
            if fname:
                print(f"[analysis] camera captured: {fname}")
                # Write filename back into mimir sidecar so web UI can display it
                sidecar = Path(recording_path).with_suffix(".json")
                if sidecar.exists():
                    try:
                        data = json.loads(sidecar.read_text())
                        data["photo"] = fname
                        sidecar.write_text(json.dumps(data, indent=2))
                    except Exception:
                        pass
        except Exception as e:
            print(f"[analysis] camera trigger failed: {e}")
    threading.Thread(target=_post, daemon=True).start()


def classify_birds(wav_path):
    """Run BirdNET on wav_path. Returns list of {label, scientific, icon, confidence, start, end}."""
    model = _get_birdnet()
    if not model:
        return []
    try:
        from birdnetlib import Recording
        from datetime import datetime
        stat = wav_path.stat()
        dt = datetime.fromtimestamp(stat.st_mtime)
        rec = Recording(
            model,
            str(wav_path),
            lat=MIMIR_LAT,
            lon=MIMIR_LON,
            date=dt,
            min_conf=0.10,
        )
        rec.analyze()
        results = []
        seen = {}
        for d in rec.detections:
            name = d["common_name"].lower()
            conf = round(d["confidence"], 2)
            if name not in seen or conf > seen[name]:
                seen[name] = conf
                results.append({
                    "label": name,
                    "scientific": d.get("scientific_name", ""),
                    "icon": BIRD_SPECIES_ICONS.get(name, "🐦"),
                    "confidence": conf,
                    "start": round(d.get("start_time", 0), 1),
                    "end": round(d.get("end_time", 3), 1),
                })
        # Deduplicate: keep highest confidence per species
        best = {}
        for r in results:
            if r["label"] not in best or r["confidence"] > best[r["label"]]["confidence"]:
                best[r["label"]] = r
        return sorted(best.values(), key=lambda x: x["confidence"], reverse=True)
    except Exception as e:
        return [{"label": "birdnet_error", "icon": "⚠", "confidence": 0, "detail": str(e)}]


# ── Whisper transcriber ─────────────────────────────────────────

_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper(model_size="tiny"):
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            try:
                from faster_whisper import WhisperModel
                print(f"[analysis] loading whisper {model_size}...")
                _whisper_model = WhisperModel(model_size, device="cpu", compute_type="int8")
                print("[analysis] whisper ready")
            except ImportError:
                print("[analysis] faster-whisper not installed — transcription disabled")
                _whisper_model = False  # sentinel: don't retry
    return _whisper_model if _whisper_model else None


def transcribe(wav_path, model_size="tiny"):
    model = _get_whisper(model_size)
    if not model:
        return None
    try:
        segments, info = model.transcribe(
            str(wav_path),
            beam_size=1,
            language=None,  # auto-detect
            condition_on_previous_text=False,
            vad_filter=True,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return {
            "text": text,
            "language": info.language,
            "language_prob": round(info.language_probability, 2),
            "duration": round(info.duration, 1),
        }
    except Exception as e:
        return {"error": str(e)}


# ── Background queue worker ─────────────────────────────────────

class Analyzer:
    def __init__(self):
        self._q = queue.Queue()
        self._current = None        # filename currently being analyzed
        self._pending = []          # filenames waiting in queue
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def enqueue(self, wav_path, cfg=None):
        p = Path(wav_path)
        with self._lock:
            self._pending.append(p.name)
        self._q.put((p, cfg or {}))

    def queue_status(self):
        """Return {current, pending_count, pending} for web UI."""
        with self._lock:
            return {
                "current": self._current,
                "pending_count": len(self._pending),
                "pending": list(self._pending),
            }

    def _worker(self):
        while True:
            wav_path, cfg = self._q.get()
            with self._lock:
                self._current = wav_path.name
                if wav_path.name in self._pending:
                    self._pending.remove(wav_path.name)
            try:
                self._run(wav_path, cfg)
            except Exception as e:
                print(f"[analysis] error on {wav_path.name}: {e}")
            finally:
                with self._lock:
                    self._current = None
            self._q.task_done()

    def _run(self, wav_path, cfg):
        sidecar = wav_path.with_suffix(".json")
        if sidecar.exists():
            return  # already done

        print(f"[analysis] analyzing {wav_path.name}")
        # Mark as in-progress
        sidecar.write_text(json.dumps({"status": "processing"}))

        result = {"status": "done"}

        # Spectral classification (always runs, fast)
        result["tags"] = classify_sounds(wav_path)

        # BirdNET species detection (if enabled in config, default on)
        if cfg.get("birdnet_enabled", True):
            bird_detections = classify_birds(wav_path)
            if bird_detections:
                result["birds"] = bird_detections
                # Promote top bird detection into tags (replacing generic "bird" tag)
                result["tags"] = [t for t in result["tags"] if t["label"] != "bird"]
                for b in bird_detections[:3]:  # top 3 species into tags
                    result["tags"].insert(0, {
                        "label": b["label"],
                        "icon": b["icon"],
                        "confidence": b["confidence"],
                        "source": "birdnet",
                    })
                result["tags"].sort(key=lambda t: t["confidence"], reverse=True)
                # Push notification via ntfy.sh
                _notify_birds(bird_detections, wav_path, cfg)

                # Fetch species images (Wikipedia, cached)
                for b in bird_detections:
                    img = fetch_species_image(b["label"])
                    if img:
                        b["image_url"] = img["url"]
                        b["image_desc"] = img.get("desc", "")

                # Capture RTSP clip only for corvids
                CORVIDS = {"american crow", "common raven", "northwestern crow",
                           "fish crow", "steller's jay", "blue jay", "clark's nutcracker"}
                rtsp_url = cfg.get("rtsp_url")
                corvid_detections = [d for d in bird_detections if d["label"] in CORVIDS]
                if rtsp_url and corvid_detections:
                    bird_detections = corvid_detections  # use corvid as top for clip label
                    try:
                        from camera import capture_clip_async
                        top = bird_detections[0]
                        def _on_clip(mp4, thumb):
                            if mp4:
                                data = json.loads(sidecar.read_text()) if sidecar.exists() else result
                                data["video"] = str(mp4)
                                if thumb:
                                    data["photo"] = str(thumb)
                                sidecar.write_text(json.dumps(data, indent=2))
                                print(f"[analysis] linked clip {mp4.name} → {wav_path.name}")
                        capture_clip_async(
                            label=top["label"],
                            confidence=top["confidence"],
                            duration=12,
                            cfg=cfg,
                            callback=_on_clip,
                        )
                    except Exception as e:
                        print(f"[analysis] camera capture error: {e}")

                # Individual crow voice fingerprinting
                for det in corvid_detections:
                    if det.get("start") is not None and det.get("end") is not None:
                        try:
                            from crow_id import identify_crow
                            crow = identify_crow(
                                wav_path, det["start"], det["end"],
                                species=det["label"],
                                confidence=det["confidence"],
                            )
                            if crow:
                                det["crow_id"] = crow["crow_id"]
                                det["crow_name"] = crow["crow_name"]
                                det["is_new_crow"] = crow["is_new"]
                                det["crow_sightings"] = crow["sighting_count"]
                                det["crow_similarity"] = crow["similarity"]
                        except Exception as e:
                            print(f"[analysis] crow ID error: {e}")

        # Whisper transcription (if enabled in config)
        if cfg.get("whisper_enabled", False):
            model_size = cfg.get("whisper_model", "tiny")
            t = transcribe(wav_path, model_size)
            if t:
                result["transcript"] = t
                # Merge whisper's speech detection into tags
                if t.get("text") and not any(t["label"] == "speech" for t in result["tags"]):
                    result["tags"].insert(0, {
                        "label": "speech", "icon": "🗣",
                        "confidence": t.get("language_prob", 0.8)
                    })

        sidecar.write_text(json.dumps(result, indent=2))
        print(f"[analysis] done {wav_path.name}: {[t['label'] for t in result['tags']]}")


# Singleton
analyzer = Analyzer()

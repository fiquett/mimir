#!/usr/bin/env python3
"""
mimir - event-driven sound monitor
Records audio when level exceeds threshold * baseline_rms.
Writes state to /run/mimir/state.json for web UI.
"""

import json
import os
import socket
import sys
import time
import wave
import threading
import collections
import numpy as np
import pyaudio
from datetime import datetime
from pathlib import Path

# Import analyzer (optional — graceful if missing)
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from analysis import analyzer as _analyzer
except Exception as _e:
    print(f"[mimir] analysis module unavailable: {_e}")
    _analyzer = None

CONFIG_PATH = Path(__file__).parent / "config.json"
STATE_PATH = Path("/run/mimir/state.json")
CAL_TRIGGER = Path("/run/mimir/calibrate.trigger")
LIVE_SOCKET = Path("/run/mimir/live.sock")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(STATE_PATH) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, str(STATE_PATH))


class SoundMonitor:
    def __init__(self):
        self.cfg = load_config()
        self.sr = self.cfg["sample_rate"]
        self.channels = self.cfg["channels"]
        self.recordings_dir = Path(self.cfg["recordings_dir"])
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

        # Pre-roll ring buffer: stores last N seconds of audio
        pre_frames = int(self.cfg["pre_roll_seconds"] * self.sr)
        self.pre_roll = collections.deque(maxlen=pre_frames)

        self.state = "idle"  # idle | recording | cooldown
        self.recording_frames = []
        self.recording_start = None
        self.post_roll_remaining = 0
        self.current_rms = 0.0
        self.last_event = None
        self.total_events = self._count_existing_recordings()
        self.lock = threading.Lock()
        self.current_mode = self.cfg.get("mode", "event")

        # Calibration
        self.calibrating = False
        self.cal_frames = []
        self.cal_started = None

        # Live listeners
        self.live_clients = []
        self.live_lock = threading.Lock()

    def _count_existing_recordings(self):
        return len(list(self.recordings_dir.rglob("*.wav")))

    def _rms(self, data):
        return float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))

    def _threshold(self):
        cfg = load_config()
        return cfg["baseline_rms"] * cfg["threshold_multiplier"]

    def _save_recording(self, frames):
        cfg = load_config()
        now = datetime.now()
        day_dir = self.recordings_dir / now.strftime("%Y-%m-%d")
        day_dir.mkdir(exist_ok=True)
        fname = now.strftime("%Y-%m-%d_%H-%M-%S") + "_event.wav"
        fpath = day_dir / fname

        audio = np.concatenate(frames)
        with wave.open(str(fpath), "w") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)  # int16
            wf.setframerate(self.sr)
            wf.writeframes(audio.astype(np.int16).tobytes())

        duration = len(audio) / self.sr
        self.last_event = {
            "time": now.isoformat(),
            "file": str(fpath),
            "duration": round(duration, 2),
            "peak_rms": round(self._rms(audio), 5),
        }
        self.total_events += 1
        print(f"[mimir] saved {fpath} ({duration:.1f}s)")
        self._trim_old_recordings(cfg)
        if _analyzer and cfg.get("analysis_enabled", True):
            _analyzer.enqueue(fpath, cfg)

    def _trim_old_recordings(self, cfg):
        if not cfg.get("round_robin"):
            return
        max_bytes = int(cfg.get("max_recordings_gb", 10) * 1024 ** 3)
        target_bytes = int(max_bytes * 0.9)  # trim to 90% to give breathing room

        wavs = sorted(self.recordings_dir.rglob("*.wav"), key=lambda f: f.stat().st_mtime)
        current = sum(f.stat().st_size for f in wavs)
        if current <= max_bytes:
            return

        for wav in wavs:
            if current <= target_bytes:
                break
            try:
                size = wav.stat().st_size
                wav.unlink()
                try:
                    wav.parent.rmdir()
                except OSError:
                    pass
                current -= size
                print(f"[mimir] round-robin: removed {wav.name}")
            except Exception:
                pass

    def process_chunk(self, chunk_int):
        cfg = load_config()
        mode = cfg.get("mode", "event")
        frames = len(chunk_int)
        rms = self._rms(chunk_int)
        self.current_rms = rms
        threshold = self._threshold()

        if not cfg.get("monitoring_enabled", True):
            save_state({
                "state": "stopped",
                "mode": cfg.get("mode", "event"),
                "rms": 0, "threshold": round(threshold, 5),
                "baseline_rms": round(cfg["baseline_rms"], 5),
                "threshold_multiplier": cfg["threshold_multiplier"],
                "total_events": self.total_events,
                "last_event": self.last_event,
                "calibrating": False, "cal_started": None,
                "ts": time.time(),
            })
            return

        with self.lock:
            if self.calibrating:
                self.cal_frames.append(chunk_int.copy())
            else:
                # Handle mode transition
                if mode != self.current_mode:
                    if self.current_mode == "continuous" and self.recording_frames:
                        threading.Thread(target=self._save_recording,
                                         args=(self.recording_frames,), daemon=True).start()
                        self.recording_frames = []
                    self.state = "idle"
                    self.current_mode = mode
                    print(f"[mimir] mode → {mode}")

                if mode == "continuous":
                    chunk_secs = cfg.get("continuous_chunk_seconds", 300)
                    if self.state != "recording":
                        self.state = "recording"
                        self.recording_start = datetime.now()
                        self.recording_frames = []
                    self.recording_frames.append(chunk_int.copy())
                    elapsed = (datetime.now() - self.recording_start).total_seconds()
                    if elapsed >= chunk_secs:
                        frames_to_save = self.recording_frames
                        # Restart immediately — no gap
                        self.recording_start = datetime.now()
                        self.recording_frames = []
                        threading.Thread(target=self._save_recording,
                                         args=(frames_to_save,), daemon=True).start()

                else:
                    self.pre_roll.extend(chunk_int.tolist())

                    if self.state == "idle":
                        if rms > threshold:
                            self.state = "recording"
                            self.recording_start = datetime.now()
                            self.recording_frames = [np.array(list(self.pre_roll))]
                            self.post_roll_remaining = cfg["post_roll_seconds"] * self.sr
                            print(f"[mimir] event started (rms={rms:.4f} > threshold={threshold:.4f})")
                            # Trigger camera immediately on event start (before BirdNET)
                            camera_url = cfg.get("camera_url")
                            if camera_url:
                                threading.Thread(
                                    target=self._trigger_camera_early,
                                    args=(camera_url, self.recording_start),
                                    daemon=True,
                                ).start()

                    elif self.state == "recording":
                        self.recording_frames.append(chunk_int.copy())
                        elapsed = (datetime.now() - self.recording_start).total_seconds()

                        if rms > threshold:
                            self.post_roll_remaining = cfg["post_roll_seconds"] * self.sr
                        else:
                            self.post_roll_remaining -= frames

                        if self.post_roll_remaining <= 0 or elapsed >= cfg["max_duration_seconds"]:
                            self.state = "idle"
                            threading.Thread(target=self._save_recording,
                                             args=(self.recording_frames,), daemon=True).start()
                            self.recording_frames = []

        save_state({
            "state": self.state,
            "mode": mode,
            "rms": round(rms, 5),
            "threshold": round(threshold, 5),
            "baseline_rms": round(cfg["baseline_rms"], 5),
            "threshold_multiplier": cfg["threshold_multiplier"],
            "total_events": self.total_events,
            "last_event": self.last_event,
            "calibrating": self.calibrating,
            "cal_started": self.cal_started,
            "ts": time.time(),
        })

    def _find_usb_device(self, pa):
        cfg = load_config()
        device_index = cfg.get("device_index")
        if device_index is not None:
            return device_index
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d["maxInputChannels"] > 0 and "USB" in d["name"]:
                print(f"[mimir] auto-selected device {i}: {d['name']}")
                return i
        return None

    def calibrate(self, duration=10):
        print(f"[mimir] calibrating for {duration}s...")
        with self.lock:
            self.calibrating = True
            self.cal_frames = []
            self.cal_started = time.time()
        time.sleep(duration)
        with self.lock:
            self.calibrating = False
            self.cal_started = None
            if self.cal_frames:
                audio = np.concatenate(self.cal_frames)
                baseline = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
                cfg = load_config()
                cfg["baseline_rms"] = round(baseline, 6)
                with open(CONFIG_PATH, "w") as f:
                    json.dump(cfg, f, indent=2)
                print(f"[mimir] baseline set to {baseline:.6f}")
                return baseline
        return None

    def _start_live_server(self):
        if LIVE_SOCKET.exists():
            LIVE_SOCKET.unlink()

        def server():
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(LIVE_SOCKET))
            srv.listen(5)
            while True:
                conn, _ = srv.accept()
                with self.live_lock:
                    self.live_clients.append(conn)

        threading.Thread(target=server, daemon=True).start()

    def _broadcast_live(self, data):
        if not self.live_clients:
            return
        dead = []
        with self.live_lock:
            for conn in self.live_clients:
                try:
                    conn.sendall(data)
                except Exception:
                    dead.append(conn)
            for conn in dead:
                self.live_clients.remove(conn)

    def _trigger_camera_early(self, camera_url, event_time):
        """Trigger camera capture at event start. BirdNET will link to it later by timestamp."""
        import urllib.request
        try:
            ts_str = event_time.strftime("%Y-%m-%d_%H-%M-%S")
            payload = json.dumps({
                "label": "sound_event",
                "confidence": 0.0,
                "event_ts": ts_str,
            }).encode()
            req = urllib.request.Request(
                f"{camera_url.rstrip('/')}/capture",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                pass
            print(f"[mimir] camera triggered at {ts_str}")
        except Exception as e:
            print(f"[mimir] camera trigger failed: {e}")

    def _check_cal_trigger(self):
        if CAL_TRIGGER.exists():
            try:
                CAL_TRIGGER.unlink()
            except OSError:
                pass
            threading.Thread(target=self.calibrate, daemon=True).start()

    def run(self):
        cfg = load_config()
        chunk_size = int(self.sr * 0.1)  # 100ms
        pa = pyaudio.PyAudio()
        device_index = self._find_usb_device(pa)

        print(f"[mimir] starting on device={device_index} sr={self.sr}")
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.sr,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=chunk_size,
        )
        self._start_live_server()
        print("[mimir] listening...")
        try:
            while True:
                raw = stream.read(chunk_size, exception_on_overflow=False)
                chunk = np.frombuffer(raw, dtype=np.int16)
                self.process_chunk(chunk)
                self._broadcast_live(raw)
                self._check_cal_trigger()
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()


if __name__ == "__main__":
    import sys
    monitor = SoundMonitor()
    if len(sys.argv) > 1 and sys.argv[1] == "calibrate":
        duration = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        cfg = load_config()
        chunk_size = int(cfg["sample_rate"] * 0.1)
        pa = pyaudio.PyAudio()
        device_index = monitor._find_usb_device(pa)
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=cfg["channels"],
            rate=cfg["sample_rate"],
            input=True,
            input_device_index=device_index,
            frames_per_buffer=chunk_size,
        )
        with monitor.lock:
            monitor.calibrating = True
            monitor.cal_frames = []
        print(f"Calibrating for {duration}s, be quiet...")
        for _ in range(int(duration * 10)):
            raw = stream.read(chunk_size, exception_on_overflow=False)
            chunk = np.frombuffer(raw, dtype=np.int16)
            monitor.process_chunk(chunk)
        stream.stop_stream(); stream.close(); pa.terminate()
        with monitor.lock:
            monitor.calibrating = False
            if monitor.cal_frames:
                audio = np.concatenate(monitor.cal_frames)
                baseline = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
                cfg = load_config()
                cfg["baseline_rms"] = round(baseline, 6)
                with open(CONFIG_PATH, "w") as f:
                    json.dump(cfg, f, indent=2)
                print(f"baseline_rms = {baseline}")
            else:
                print("No frames collected")
    else:
        monitor.run()

# mimir

Outdoor bird monitoring system for Raspberry Pi. Named after the Norse god of wisdom.

Two-Pi architecture:
- **mimir Pi** — audio capture, BirdNET species identification (local ML), spectral sound classification, Flask web UI
- **uplink Pi** — USB webcam / RTSP camera video capture, linked to audio events by timestamp

## Features

- Real-time sound monitoring with configurable thresholds
- BirdNET ML bird species ID (runs locally via TFLite, ~6000 species)
- Wikipedia species images auto-fetched and cached
- Waveform audio player with click-to-seek on bird detections
- Stats page with hourly heatmap, species leaderboard, daily breakdown
- Public bird feed page (speech-filtered, no auth required)
- Push notifications via ntfy.sh with direct clip links
- Camera integration — video clips linked to audio events
- PIN-protected dashboard
- USB drive support for recordings storage

## Setup

### mimir Pi (audio + web)

```bash
pip install -r requirements.txt
cp config.example.json config.json
# Edit config.json with your settings
python monitor.py   # sound monitor daemon
python web.py       # web UI (port 8765)
```

### uplink Pi (camera)

```bash
pip install flask
python camera_server.py  # camera server (port 8766)
```

### systemd services

```ini
# /etc/systemd/system/mimir-monitor.service
[Unit]
Description=mimir sound monitor
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/mimir
ExecStart=/usr/bin/python3 /home/pi/mimir/monitor.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/mimir-camera.service
[Unit]
Description=mimir camera server
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi
ExecStart=/usr/bin/python3 /home/pi/camera_server.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

## Hardware

- Raspberry Pi 4/5 (64-bit OS required for BirdNET)
- USB microphone (or audio interface + lavalier mic)
- USB webcam or RTSP IP camera (optional)
- USB drive for recordings storage (recommended)

## License

MIT

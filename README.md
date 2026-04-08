# mimir

Outdoor bird monitoring system for Raspberry Pi. Named after the Norse god of wisdom.

Listens for birds, identifies species with BirdNET (local ML), and serves a live web dashboard with waveform playback, species images, stats, and push notifications.

## Features

- Real-time sound monitoring with configurable thresholds
- BirdNET ML species ID — runs locally via TFLite, ~6000 species, no cloud
- Spectral sound classification (aircraft, rain, wind, speech, etc.)
- Wikipedia species images auto-fetched and cached
- Waveform audio player with click-to-seek on bird detections
- Stats page with hourly heatmap, species leaderboard, daily breakdown
- Public bird feed page — speech-filtered, safe to share, no auth required
- Push notifications via ntfy.sh with direct clip links
- RTSP camera integration (optional)
- PIN-protected dashboard
- USB drive support for recordings storage

## Setup

```bash
pip install -r requirements.txt
cp config.example.json config.json
# Edit config.json with your settings
python monitor.py   # sound monitor daemon
python web.py       # web UI (port 8765)
```

### systemd service

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

Create a matching `mimir-web.service` pointing to `web.py`.

## Hardware

- Raspberry Pi 4/5 (64-bit OS required for BirdNET)
- USB microphone (or audio interface + lavalier mic for better sensitivity)
- RTSP IP camera for video clips (optional)
- USB drive for recordings storage (recommended — saves SD card wear)

## License

MIT

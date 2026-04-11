#!/usr/bin/env python3
"""
mimir crow_id — individual crow voice fingerprinting via MFCC embeddings.

Extracts vocal fingerprints from BirdNET-detected crow calls, stores them
in SQLite, and matches against known individuals using cosine similarity.
"""

import json
import sqlite3
import threading
import wave
import numpy as np
from pathlib import Path
from datetime import datetime

DB_PATH = Path("/mnt/usb/crow_id.db")
_db_lock = threading.Lock()

# Similarity threshold: above this = same crow
# Higher = stricter matching, fewer false merges but more fragmentation
MATCH_THRESHOLD = 0.92
# Minimum detections to consider a crow "established"
MIN_SIGHTINGS = 3

CORVID_SPECIES = {"american crow", "common raven", "northwestern crow",
                   "fish crow", "steller's jay"}


def _send_ntfy(title, body):
    """Send push notification via ntfy.sh."""
    try:
        cfg_path = Path("/home/pi/mimir/config.json")
        if not cfg_path.exists():
            return
        cfg = json.loads(cfg_path.read_text())
        topic = cfg.get("ntfy_topic", "").strip()
        if not topic:
            return
        import urllib.request
        base_url = cfg.get("tailscale_url") or cfg.get("local_url") or "http://localhost:8765"
        payload = json.dumps({
            "topic": topic, "title": title, "message": body,
            "priority": 4, "tags": ["crow"],
            "click": f"{base_url}/crows",
            "actions": [{"action": "view", "label": "View Corvids", "url": f"{base_url}/crows"}],
        }).encode()
        req = urllib.request.Request(
            "https://ntfy.sh", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass

# Name generator — Norse/Celtic mythology themed for corvids
CROW_NAMES = [
    # Norse mythology
    "Huginn", "Muninn", "Odin", "Loki", "Freya", "Thor", "Fenrir", "Baldur",
    "Skadi", "Tyr", "Bragi", "Njord", "Vidar", "Vali", "Forseti", "Heimdall",
    "Sigyn", "Nanna", "Sif", "Idunn", "Frigg", "Hel", "Jormungandr",
    # Celtic mythology
    "Morrigan", "Badb", "Macha", "Branwen", "Bran", "Dagda", "Brigid",
    "Lugh", "Cernunnos", "Rhiannon", "Arawn", "Taliesin", "Cerridwen",
    # Corvid themed
    "Obsidian", "Shadow", "Onyx", "Midnight", "Coal", "Ash", "Storm",
    "Flint", "Rook", "Jet", "Ember", "Dusk", "Slate", "Rune", "Talon",
    "Cinder", "Sable", "Raven", "Phantom", "Eclipse", "Void", "Nox",
    "Vesper", "Thorn", "Wisp", "Grimm", "Wraith", "Specter", "Shade",
    # Trickster/clever
    "Trickster", "Kakaw", "Wyrd", "Riddle", "Gambit", "Wager", "Cipher",
    "Jinx", "Rascal", "Bandit", "Prowl", "Scout", "Sage", "Oracle",
    # Nature
    "Cedar", "Hemlock", "Alder", "Birch", "Rowan", "Hawthorn", "Thistle",
    "Bracken", "Lichen", "Moss", "Fern", "Pine", "Spruce", "Madrone",
    # Seattle/PNW
    "Rainier", "Cascade", "Puget", "Orca", "Salish", "Olympic", "Skagit",
    "Snoqualmie", "Duwamish", "Tahoma", "Chinook", "Tillamook",
]

def _generate_name(crow_id, species):
    """Generate a themed name for a returning corvid."""
    idx = (crow_id - 1) % len(CROW_NAMES)
    return CROW_NAMES[idx]


def _init_db():
    """Create tables if they don't exist."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS crows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            species TEXT DEFAULT 'american crow',
            first_seen TEXT,
            last_seen TEXT,
            sighting_count INTEGER DEFAULT 0,
            avg_embedding BLOB,
            notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            crow_id INTEGER,
            timestamp TEXT,
            wav_path TEXT,
            start_sec REAL,
            end_sec REAL,
            embedding BLOB,
            confidence REAL,
            FOREIGN KEY (crow_id) REFERENCES crows(id)
        );
    """)
    conn.close()


def _extract_mfcc(wav_path, start_sec=0, end_sec=None, n_mfcc=26):
    """Extract detailed vocal fingerprint from a wav segment. Returns a 1D numpy array."""
    try:
        import librosa

        # Load the specific segment
        y, sr = librosa.load(str(wav_path), sr=22050,
                             offset=start_sec,
                             duration=(end_sec - start_sec) if end_sec else None,
                             mono=True)

        if len(y) < sr * 0.5:  # less than 0.5s, too short
            return None

        # Shorter hop for finer temporal resolution
        n_fft = 1024
        hop = 256

        # MFCCs — more coefficients for better discrimination
        mfccs = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=n_mfcc,
                                      n_fft=n_fft, hop_length=hop)
        delta = librosa.feature.delta(mfccs)
        delta2 = librosa.feature.delta(mfccs, order=2)

        # Spectral features — capture tonal quality unique to individual
        spectral_centroid = librosa.feature.spectral_centroid(y=y, sr=sr,
                                                              n_fft=n_fft, hop_length=hop)
        spectral_bandwidth = librosa.feature.spectral_bandwidth(y=y, sr=sr,
                                                                 n_fft=n_fft, hop_length=hop)
        spectral_rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr,
                                                             n_fft=n_fft, hop_length=hop)
        spectral_contrast = librosa.feature.spectral_contrast(y=y, sr=sr,
                                                               n_fft=n_fft, hop_length=hop)

        # Pitch (F0) — individual crows have distinct pitch ranges
        f0 = librosa.yin(y, fmin=100, fmax=4000, sr=sr, hop_length=hop)
        f0_clean = f0[f0 > 0]  # remove unvoiced frames

        # Temporal envelope — call shape/rhythm
        envelope = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop)).mean(axis=0)
        env_norm = envelope / (envelope.max() + 1e-10)

        # Build feature vector with percentiles for robustness
        features = np.concatenate([
            # MFCC stats (mean, std, skew via percentiles)
            mfccs.mean(axis=1),
            mfccs.std(axis=1),
            np.percentile(mfccs, 10, axis=1),
            np.percentile(mfccs, 90, axis=1),
            # Delta MFCC
            delta.mean(axis=1),
            delta.std(axis=1),
            # Delta-delta MFCC
            delta2.mean(axis=1),
            delta2.std(axis=1),
            # Spectral shape
            [spectral_centroid.mean(), spectral_centroid.std()],
            [spectral_bandwidth.mean(), spectral_bandwidth.std()],
            [spectral_rolloff.mean(), spectral_rolloff.std()],
            spectral_contrast.mean(axis=1),
            spectral_contrast.std(axis=1),
            # Pitch
            [f0_clean.mean() if len(f0_clean) else 0,
             f0_clean.std() if len(f0_clean) > 1 else 0,
             np.median(f0_clean) if len(f0_clean) else 0,
             np.percentile(f0_clean, 10) if len(f0_clean) else 0,
             np.percentile(f0_clean, 90) if len(f0_clean) else 0],
            # Envelope shape
            [env_norm.mean(), env_norm.std(),
             np.percentile(env_norm, 25), np.percentile(env_norm, 75)],
        ])

        # Normalize to unit vector
        norm = np.linalg.norm(features)
        if norm > 0:
            features = features / norm

        return features.astype(np.float32)

    except Exception as e:
        print(f"[crow_id] MFCC extraction error: {e}")
        return None


def _cosine_similarity(a, b):
    """Cosine similarity between two vectors."""
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def _embedding_to_blob(arr):
    return arr.tobytes()


def _blob_to_embedding(blob):
    return np.frombuffer(blob, dtype=np.float32)


def identify_crow(wav_path, start_sec, end_sec, species="american crow", confidence=0.0):
    """
    Identify which individual crow made this call.
    Returns {crow_id, crow_name, is_new, similarity, sighting_count} or None.
    """
    with _db_lock:
        _init_db()

        embedding = _extract_mfcc(wav_path, start_sec, end_sec)
        if embedding is None:
            return None

        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        # Compare against all known crows of this species
        crows = conn.execute(
            "SELECT id, name, avg_embedding, sighting_count FROM crows WHERE species = ?",
            (species,)
        ).fetchall()

        best_match = None
        best_sim = 0.0

        for crow in crows:
            if crow["avg_embedding"]:
                known_emb = _blob_to_embedding(crow["avg_embedding"])
                sim = _cosine_similarity(embedding, known_emb)
                if sim > best_sim:
                    best_sim = sim
                    best_match = crow

        now = datetime.now().isoformat()

        if best_match and best_sim >= MATCH_THRESHOLD:
            # Known crow — update
            crow_id = best_match["id"]
            count = best_match["sighting_count"] + 1

            # Update running average embedding
            old_emb = _blob_to_embedding(best_match["avg_embedding"])
            # Weighted average: give more weight to established embeddings
            weight = min(count - 1, 20)  # cap at 20 past sightings
            new_avg = (old_emb * weight + embedding) / (weight + 1)
            new_avg = new_avg / np.linalg.norm(new_avg)  # re-normalize

            crow_name = best_match["name"]
            # Auto-generate a name on second sighting if still just an ID number
            if count == 2 and crow_name.startswith("Corvid #"):
                crow_name = _generate_name(crow_id, species)
                print(f"[crow_id] named {best_match['name']} -> {crow_name}")
                threading.Thread(target=_send_ntfy,
                    args=(f"🐦‍⬛ {crow_name} earned a name!",
                          f"{best_match['name']} is now {crow_name} ({species})\nSecond sighting confirmed — this one's a regular."),
                    daemon=True).start()

            # Recompute representative embedding from last N sightings (median is more robust than mean)
            recent = conn.execute(
                "SELECT embedding FROM sightings WHERE crow_id = ? ORDER BY timestamp DESC LIMIT 15",
                (crow_id,)
            ).fetchall()
            if recent:
                all_emb = np.array([_blob_to_embedding(r[0]) for r in recent])
                new_repr = np.median(all_emb, axis=0)
                new_repr = new_repr / (np.linalg.norm(new_repr) + 1e-10)
            else:
                new_repr = new_avg

            conn.execute(
                "UPDATE crows SET last_seen=?, sighting_count=?, avg_embedding=?, name=? WHERE id=?",
                (now, count, _embedding_to_blob(new_repr.astype(np.float32)), crow_name, crow_id)
            )
            is_new = False

        else:
            # New corvid — assign ID number, name comes when they return
            total = conn.execute("SELECT COUNT(*) FROM crows").fetchone()[0]
            crow_name = f"Corvid #{total + 1}"
            conn.execute(
                "INSERT INTO crows (name, species, first_seen, last_seen, sighting_count, avg_embedding) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (crow_name, species, now, now, _embedding_to_blob(embedding))
            )
            crow_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            count = 1
            is_new = True
            threading.Thread(target=_send_ntfy,
                args=(f"✨ New corvid detected!",
                      f"{crow_name} — {species}\nFirst time hearing this individual. Listening for a return visit to confirm and name them."),
                daemon=True).start()

        # Record sighting
        conn.execute(
            "INSERT INTO sightings (crow_id, timestamp, wav_path, start_sec, end_sec, embedding, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (crow_id, now, str(wav_path), start_sec, end_sec,
             _embedding_to_blob(embedding), confidence)
        )
        conn.commit()
        conn.close()

        result = {
            "crow_id": crow_id,
            "crow_name": crow_name,
            "is_new": is_new,
            "similarity": round(best_sim, 3),
            "sighting_count": count,
            "species": species,
        }
        action = "NEW" if is_new else f"matched (sim={best_sim:.3f})"
        print(f"[crow_id] {crow_name} — {action}, sighting #{count}")

        # Regenerate spectrogram in background
        threading.Thread(target=_regenerate_spectrogram,
                         args=(crow_id, crow_name, species), daemon=True).start()

        return result


def _regenerate_spectrogram(crow_id, name, species):
    """Regenerate cached spectrogram PNG for a corvid."""
    try:
        import librosa, librosa.display
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT wav_path, start_sec, end_sec FROM sightings "
            "WHERE crow_id = ? ORDER BY confidence DESC LIMIT 1", (crow_id,)
        ).fetchone()
        conn.close()
        if not row:
            return
        wav_path, start, end = row
        if not Path(wav_path).exists():
            return

        cache_dir = Path("/mnt/usb/cache/spectrograms")
        cache_dir.mkdir(parents=True, exist_ok=True)

        y, sr = librosa.load(wav_path, sr=22050, offset=start, duration=end - start, mono=True)
        fig, ax = plt.subplots(figsize=(6, 3), dpi=150)
        S = librosa.feature.melspectrogram(y=y, sr=sr, n_mels=128, fmax=8000, hop_length=256)
        S_dB = librosa.power_to_db(S, ref=np.max)
        librosa.display.specshow(S_dB, sr=sr, hop_length=256, x_axis="time", y_axis="mel",
                                  ax=ax, cmap="magma", vmin=-60, vmax=0)
        title = f"{name} \u2014 {species.title()}" if species else name
        ax.set_title(title, fontsize=10, color="#d2a8ff", fontweight="bold")
        ax.set_xlabel("")
        ax.set_ylabel("Hz", fontsize=8, color="#8b949e")
        ax.tick_params(labelsize=7, colors="#8b949e")
        fig.patch.set_facecolor("#0d1117")
        ax.set_facecolor("#0d1117")
        for spine in ax.spines.values():
            spine.set_color("#30363d")
        plt.tight_layout()
        out = cache_dir / f"crow_{crow_id}.png"
        plt.savefig(str(out), facecolor="#0d1117", bbox_inches="tight")
        plt.close(fig)
        print(f"[crow_id] spectrogram updated for {name}")
    except Exception as e:
        print(f"[crow_id] spectrogram error: {e}")


def get_all_crows(species=None):
    """Return list of all known crows with stats."""
    _init_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    if species:
        rows = conn.execute(
            "SELECT id, name, species, first_seen, last_seen, sighting_count, notes "
            "FROM crows WHERE species = ? ORDER BY sighting_count DESC", (species,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, species, first_seen, last_seen, sighting_count, notes "
            "FROM crows ORDER BY sighting_count DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_crow_sightings(crow_id, limit=50):
    """Return recent sightings for a specific crow."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT timestamp, wav_path, start_sec, end_sec, confidence "
        "FROM sightings WHERE crow_id = ? ORDER BY timestamp DESC LIMIT ?",
        (crow_id, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def rename_crow(crow_id, new_name):
    """Give a crow a custom name."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE crows SET name = ? WHERE id = ?", (new_name, crow_id))
    conn.commit()
    conn.close()


# ── CLI test ────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 4:
        wav = sys.argv[1]
        start = float(sys.argv[2])
        end = float(sys.argv[3])
        result = identify_crow(wav, start, end)
        print(json.dumps(result, indent=2))
    else:
        crows = get_all_crows()
        if crows:
            print(f"\n{len(crows)} known crows:")
            for c in crows:
                print(f"  {c['name']} — {c['sighting_count']} sightings, "
                      f"last seen {c['last_seen'][:16]}")
        else:
            print("No crows identified yet.")

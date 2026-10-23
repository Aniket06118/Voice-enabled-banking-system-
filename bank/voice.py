"""
Voice security: "is this really the account owner, and are they really speaking
right now?"  Two independent checks:

  1. SPEAKER CHECK   - a voiceprint (ECAPA embedding from SpeechBrain, the same
                       model your friend's code uses) compared with the enrolled one.
  2. LIVENESS CHECK  - the user must read out random digits we choose at that
                       moment. The digits are transcribed SERVER-side (Gemini) from
                       the audio itself, so a replayed old recording can't pass.

Heavy libraries (torch, speechbrain) are imported lazily, only when a real
embedding is needed, so the rest of the app starts fast and tests can swap them.
"""
import importlib.util
import io
import logging
import os
import re
import threading
import unicodedata
import wave

import numpy as np
from django.conf import settings

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000        # the voice model expects 16 kHz mono
MIN_SECONDS = 1.5
MAX_SECONDS = 12.0
MIN_RMS = 0.004            # below this the recording is basically silence

DIGITS_PROMPT = (
    "This audio is a person reading out a short sequence of digits (it may be in "
    "English or Hindi). Reply with ONLY the digits they said, as numerals with no "
    "spaces or punctuation. If you hear no digits, reply with the single word NONE."
)


class VoiceError(Exception):
    """A problem with the audio or the voice engine. `message` is safe to show."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ audio

def decode_wav(data: bytes) -> np.ndarray:
    """16-bit PCM WAV bytes -> float32 mono samples at 16 kHz (no ffmpeg needed)."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        raise VoiceError("audio_invalid", "I couldn't read that recording. Please try again.")
    if width != 2:
        raise VoiceError("audio_invalid", "Unsupported audio format.")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples[: len(samples) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE and len(samples):
        n_out = int(len(samples) * SAMPLE_RATE / rate)
        samples = np.interp(np.linspace(0, len(samples) - 1, n_out),
                            np.arange(len(samples)), samples).astype(np.float32)
    return samples


def check_quality(samples: np.ndarray) -> None:
    seconds = len(samples) / SAMPLE_RATE
    if seconds < MIN_SECONDS:
        raise VoiceError("audio_too_short", "That was too short. Please read all the digits.")
    if seconds > MAX_SECONDS:
        raise VoiceError("audio_too_long", "That recording was too long. Please try again.")
    if float(np.sqrt(np.mean(samples ** 2))) < MIN_RMS:
        raise VoiceError("audio_silent", "I couldn't hear anything. Check your microphone and try again.")


MIN_SPEECH_SECONDS = 1.0     # after trimming silence, at least this much real speech
THRESHOLD_FLOOR = 0.32       # never accept a voice scoring below this, however noisy enrollment was
DEFAULT_THRESHOLD = 0.40


def prepare(samples: np.ndarray) -> np.ndarray:
    """Clean a recording before it is compared: cut the silence at both ends and
    even out the loudness. Silence and volume differences (not the voice) were a
    big reason the same person scored low on different days."""
    frame = SAMPLE_RATE // 50                      # 20 ms frames
    n = len(samples) // frame
    if n < 5:
        raise VoiceError("audio_too_short", "That was too short. Please read all the digits.")
    rms = np.sqrt(np.mean(samples[: n * frame].reshape(n, frame) ** 2, axis=1))
    gate = max(0.008, 0.12 * float(np.percentile(rms, 95)))
    active = np.where(rms > gate)[0]
    if len(active) == 0:
        raise VoiceError("audio_silent", "I couldn't hear anything. Check your microphone and try again.")
    start = max(int(active[0]) - 5, 0) * frame     # keep 100 ms around the speech
    end = min(int(active[-1]) + 6, n) * frame
    speech = samples[start:end].astype(np.float32)
    if len(speech) / SAMPLE_RATE < MIN_SPEECH_SECONDS:
        raise VoiceError("audio_too_short", "That was too short. Please read all the digits.")
    level = float(np.sqrt(np.mean(speech ** 2)))
    if level > 0:
        speech = speech * (0.1 / level)            # same loudness every time
        peak = float(np.max(np.abs(speech)))
        if peak > 0.98:
            speech = speech * (0.98 / peak)
    return speech


def cosine(a, b) -> float:
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def genuine_scores(embeddings) -> list:
    """How well does each enrollment clip match the others? (the owner vs. themselves)"""
    normed = [np.asarray(e, dtype=np.float64) / np.linalg.norm(e) for e in embeddings]
    out = []
    for i, e in enumerate(normed):
        others = np.mean([x for j, x in enumerate(normed) if j != i], axis=0)
        out.append(cosine(others, e))
    return out


def personal_threshold(embeddings, cap: float) -> float:
    """A cut-off fitted to THIS person: someone who scores 0.8 against themselves
    gets a stricter line than someone on a noisy laptop mic at 0.5. Always between
    THRESHOLD_FLOOR and `cap`."""
    typical = float(np.mean(genuine_scores(embeddings)))
    return round(min(max(typical - 0.25, THRESHOLD_FLOOR), max(cap, THRESHOLD_FLOOR)), 3)


def blend(old, new, weight: int) -> list:
    """Nudge the saved voiceprint a little toward a fresh, confident sample, so it
    keeps up with the person's real microphone and voice (continuous enrolment)."""
    a = np.asarray(old, dtype=np.float64); a = a / np.linalg.norm(a)
    b = np.asarray(new, dtype=np.float64); b = b / np.linalg.norm(b)
    m = a * weight + b
    return (m / np.linalg.norm(m)).tolist()


def digits_close(spoken: str, expected: str) -> bool:
    """Exact match, or exactly one wrong digit (speech-to-text slips happen)."""
    return len(spoken) == len(expected) and sum(x != y for x, y in zip(spoken, expected)) <= 1


def mean_embedding(embeddings) -> list:
    m = np.mean([np.asarray(e, dtype=np.float64) / np.linalg.norm(e) for e in embeddings], axis=0)
    return (m / np.linalg.norm(m)).tolist()


def consistency(embeddings) -> float:
    """How alike are the enrollment clips? Each clip is compared with the average
    of the OTHER clips (so one different speaker can't hide in the average).
    Returns the worst score; same person is typically 0.6+, different people ~0.2."""
    normed = [np.asarray(e, dtype=np.float64) / np.linalg.norm(e) for e in embeddings]
    worst = 1.0
    for i, e in enumerate(normed):
        others = np.mean([x for j, x in enumerate(normed) if j != i], axis=0)
        worst = min(worst, cosine(others, e))
    return worst


# ------------------------------------------------------------------ the model

_classifier = None
_lock = threading.Lock()


def engine_available() -> bool:
    return all(importlib.util.find_spec(m) for m in ("torch", "speechbrain"))


def _load_classifier():
    global _classifier
    with _lock:
        if _classifier is None:
            from speechbrain.inference.speaker import EncoderClassifier
            from speechbrain.utils.fetching import LocalStrategy
            log.info("Loading speaker verification model...")
            _classifier = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir=str(settings.VOICE_MODEL_DIR),
                local_strategy=LocalStrategy.COPY,   # avoids the Windows symlink problem
            )
        return _classifier


def warm_up() -> None:
    """Start loading the model in the background (the first load takes a while)."""
    if _classifier is None and engine_available():
        threading.Thread(target=_safe_load, daemon=True).start()


def _safe_load():
    try:
        _load_classifier()
    except Exception:  # noqa: BLE001
        log.exception("Voice model failed to load")


def embed_signal(samples: np.ndarray) -> np.ndarray:
    """16 kHz mono float samples -> voiceprint vector (192 numbers)."""
    if not engine_available():
        raise VoiceError("engine_missing",
                         "The voice engine isn't installed on the server. "
                         "Run: pip install torch torchaudio speechbrain")
    import torch
    classifier = _load_classifier()
    with torch.no_grad():
        emb = classifier.encode_batch(torch.from_numpy(samples).float().unsqueeze(0))
    return emb.squeeze().cpu().numpy()


# ------------------------------------------------------------------ liveness

def only_digits(text: str) -> str:
    """'4 7 २ 9' -> '4729'. Hindi (Devanagari) numerals are converted to normal ones."""
    return "".join(str(unicodedata.digit(c)) for c in (text or "") if c.isdigit())


def stt_models() -> list:
    """Main model first, then a backup (Gemini sometimes answers 503 'high demand')."""
    main = os.getenv("VOICE_STT_MODEL", "gemini-2.5-flash")
    backup = os.getenv("VOICE_STT_FALLBACK_MODEL", "gemini-2.5-flash-lite")
    return [main] + ([backup] if backup and backup != main else [])


def transcribe_digits(wav_bytes: bytes) -> str:
    """Ask Gemini which digits were spoken in this audio. Returns e.g. '472913'."""
    last = None
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(http_options=types.HttpOptions(timeout=12000))  # 12 s per try
        part = types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav")
        for model in stt_models():
            try:
                resp = client.models.generate_content(model=model, contents=[part, DIGITS_PROMPT])
                return only_digits(resp.text or "")
            except Exception as exc:  # noqa: BLE001  (503 / timeout -> try the backup model)
                last = exc
                log.warning("Digit transcription failed with %s: %s", model, exc)
    except Exception as exc:  # noqa: BLE001
        last = exc
    log.error("Digit transcription failed: %s", last)
    raise VoiceError("stt_unavailable",
                     "I couldn't check the spoken digits right now. "
                     "You can use your PIN instead, or try again in a moment.") from last

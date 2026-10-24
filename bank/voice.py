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
MIN_SNR_DB = 10.5          # speech must stand out from the background (noise alone measures ~9; a loud hall ~12-18)

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


def encode_wav(samples: np.ndarray) -> bytes:
    """float32 mono samples at 16 kHz -> 16-bit PCM WAV bytes (reverse of decode_wav)."""
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def check_quality(samples: np.ndarray) -> None:
    seconds = len(samples) / SAMPLE_RATE
    if seconds < MIN_SECONDS:
        raise VoiceError("audio_too_short", "That was too short. Please read all the digits.")
    if seconds > MAX_SECONDS:
        raise VoiceError("audio_too_long", "That recording was too long. Please try again.")
    if float(np.sqrt(np.mean(samples ** 2))) < MIN_RMS:
        raise VoiceError("audio_silent", "I couldn't hear anything. Check your microphone and try again.")
    if speech_to_noise_db(samples) < MIN_SNR_DB:
        # A clip that is mostly background noise gives a "voiceprint" of the room and the
        # microphone, not of the person - and then EVERYONE in that room looks alike.
        raise VoiceError("audio_noisy", "There's too much background noise. Move somewhere quieter, "
                                        "or hold the mic closer, and read the number again.")


def speech_to_noise_db(samples: np.ndarray) -> float:
    """How far the loud (speech) parts stand above the quiet (background) parts, in dB.
    Calibrated on real speech: clean ~40, noisy room ~18-23, background noise alone ~9."""
    frame = SAMPLE_RATE // 50
    n = len(samples) // frame
    if n < 10:
        return 99.0
    rms = np.sqrt(np.mean(samples[: n * frame].reshape(n, frame) ** 2, axis=1)) + 1e-9
    return float(20 * np.log10(np.percentile(rms, 90) / np.percentile(rms, 10)))


MIN_SPEECH_SECONDS = 1.0     # after trimming silence, at least this much real speech
# Measured on real recordings through this exact pipeline (trim -> ECAPA -> cosine), with a
# shared laptop mic, room echo and background noise added: the SAME person scored 0.42-0.66
# (5th-95th percentile), a DIFFERENT person at most 0.35. So nothing under 0.40 is ever accepted.
THRESHOLD_FLOOR = 0.40
DEFAULT_THRESHOLD = 0.42
OTHER_VOICE_MARGIN = 0.05    # must match the owner clearly better than ANY other enrolled voice
SAME_VOICE_ELSEWHERE = 0.55  # a new voiceprint this close to another account's is the same person


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
    return round(min(max(typical - 0.20, THRESHOLD_FLOOR), max(cap, THRESHOLD_FLOOR)), 3)


def best_other_match(emb, account_id, limit: int = 500) -> float:
    """Highest similarity between this clip and any OTHER account's voiceprint."""
    from .models import VoiceProfile
    best = 0.0
    for other in VoiceProfile.objects.exclude(account_id=account_id).order_by("-updated_at") \
            .values_list("embedding", flat=True)[:limit]:
        try:
            best = max(best, cosine(other, emb))
        except (ValueError, TypeError):          # a voiceprint saved by different code / size
            continue
    return best


def blend(old, new, weight: int) -> list:
    """Nudge the saved voiceprint a little toward a fresh, confident sample, so it
    keeps up with the person's real microphone and voice (continuous enrolment)."""
    a = np.asarray(old, dtype=np.float64); a = a / np.linalg.norm(a)
    b = np.asarray(new, dtype=np.float64); b = b / np.linalg.norm(b)
    m = a * weight + b
    return (m / np.linalg.norm(m)).tolist()


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance: how many digits were swapped, missed or added."""
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def digits_close(spoken: str, expected: str) -> bool:
    """Exact, or ONE slip: a misheard, missed or extra digit (speech-to-text slips happen).
    Only forgiven when the voice itself is clearly the owner (see views.voice_verify)."""
    return bool(spoken) and len(spoken) >= len(expected) - 1 and edit_distance(spoken, expected) <= 1


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


# Spoken number words -> digits, so "four seven", "char saat", "चार सात" and
# "double five" all count. Speech-to-text often writes words instead of numerals.
DIGIT_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "shunya": "0", "shoonya": "0", "sunya": "0", "शून्य": "0", "जीरो": "0",
    "one": "1", "won": "1", "ek": "1", "एक": "1", "वन": "1",
    "two": "2", "to": "2", "too": "2", "do": "2", "दो": "2", "टू": "2",
    "three": "3", "tree": "3", "teen": "3", "tin": "3", "तीन": "3", "थ्री": "3",
    "four": "4", "for": "4", "char": "4", "chaar": "4", "चार": "4", "फोर": "4",
    "five": "5", "paanch": "5", "panch": "5", "paach": "5", "पांच": "5", "पाँच": "5", "फाइव": "5",
    "six": "6", "chhe": "6", "che": "6", "chhah": "6", "chah": "6", "chheh": "6", "छह": "6", "छः": "6", "छे": "6", "सिक्स": "6",
    "seven": "7", "saat": "7", "sat": "7", "सात": "7", "सेवन": "7",
    "eight": "8", "ate": "8", "aath": "8", "aat": "8", "आठ": "8", "एट": "8",
    "nine": "9", "nau": "9", "nao": "9", "नौ": "9", "नाइन": "9",
}
REPEAT_WORDS = {"double": 2, "triple": 3, "डबल": 2, "ट्रिपल": 3}


def words_to_digits(text: str) -> str:
    """Turn a transcript into the digit string it contains (numerals and number words)."""
    out, repeat = [], 1
    for tok in re.findall(r"[^\s,.;:!?।\-]+", str(text or "").lower()):
        if tok in REPEAT_WORDS:
            repeat = REPEAT_WORDS[tok]
            continue
        if any(c.isdigit() for c in tok):
            got = only_digits(tok)
        else:
            got = DIGIT_WORDS.get(tok, "")
        if got:
            out.append(got[0] * repeat + got[1:])
        repeat = 1
    return "".join(out)


def stt_models() -> list:
    """Main model first, then a backup (Gemini sometimes answers 503 'high demand')."""
    main = os.getenv("VOICE_STT_MODEL", "gemini-3.1-flash-lite")
    backup = os.getenv("VOICE_STT_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    return [main] + ([backup] if backup and backup != main else [])


STT_UNAVAILABLE = ("I couldn't check the spoken digits right now. "
                   "You can use your PIN instead, or try again in a moment.")


def transcribe_digits(wav_bytes: bytes) -> str:
    """Ask Gemini which digits were spoken in this audio. Returns e.g. '472913'."""
    last = None
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(http_options=types.HttpOptions(timeout=10000))  # 10 s per try
        part = types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav")
        for model in stt_models():
            try:
                resp = client.models.generate_content(model=model, contents=[part, DIGITS_PROMPT])
                text = resp.text or ""
                return only_digits(text) or words_to_digits(text)
            except Exception as exc:  # noqa: BLE001  (503 / timeout -> try the backup model)
                last = exc
                log.warning("Digit transcription failed with %s: %s", model, exc)
    except Exception as exc:  # noqa: BLE001
        last = exc
    log.error("Digit transcription failed: %s", last)
    raise VoiceError("stt_unavailable", STT_UNAVAILABLE) from last


def transcribe_digits_sarvam(wav_bytes: bytes) -> str:
    """Same job with Sarvam's Indian-language speech-to-text (usually ~1 s, good with
    Indian accents and Hindi number words)."""
    try:
        from sarvamai import SarvamAI
        client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])
        resp = client.speech_to_text.transcribe(
            file=("digits.wav", wav_bytes, "audio/wav"), model="saaras:v4", mode="transcribe")
        return words_to_digits(resp.transcript or "")
    except Exception as exc:  # noqa: BLE001
        log.warning("Sarvam digit transcription failed: %s", exc)
        raise VoiceError("stt_unavailable", STT_UNAVAILABLE) from exc


def stt_engines() -> list:
    """Which speech-to-text services check the digits. Both run AT THE SAME TIME;
    the first one that hears the right digits wins (faster AND fewer mishearings)."""
    wanted = getattr(settings, "VOICE_STT_ENGINES", ["sarvam", "gemini"])
    engines = []
    for name in wanted:
        if name == "sarvam" and os.getenv("SARVAM_API_KEY"):
            engines.append(transcribe_digits_sarvam)
        elif name == "gemini":
            engines.append(transcribe_digits)
    return engines or [transcribe_digits]


_stt_pool = None


def heard_digits(wav_bytes: bytes, expected: str, timeout: float = 22.0) -> str:
    """Run every engine in parallel. Returns as soon as one hears `expected`
    exactly; otherwise the closest thing any engine heard. Raises stt_unavailable
    only if every engine failed."""
    global _stt_pool
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutTimeout
    if _stt_pool is None:
        _stt_pool = ThreadPoolExecutor(max_workers=6)
    engines = stt_engines()
    if len(engines) == 1:
        return engines[0](wav_bytes)
    futures = [_stt_pool.submit(fn, wav_bytes) for fn in engines]
    best, last_err = None, None
    try:
        for fut in as_completed(futures, timeout=timeout):
            try:
                got = fut.result()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
            if got == expected:
                return got
            if best is None or edit_distance(got, expected) < edit_distance(best, expected):
                best = got
    except FutTimeout:
        log.warning("Digit transcription: some engines were too slow")
    if best is None:
        raise VoiceError("stt_unavailable", STT_UNAVAILABLE) from last_err
    return best

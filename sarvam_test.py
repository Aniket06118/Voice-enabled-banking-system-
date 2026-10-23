"""
Standalone Sarvam AI smoke test.

Run with:  uv run python sarvam_test.py

What it does:
  1. Calls Sarvam TTS (bulbul:v4-flash) to synthesise a Hindi sentence and
     saves the result to sarvam_tts_out.wav.
  2. Calls Sarvam STT (saaras:v4) on that WAV file and prints the transcript.

The SARVAM_API_KEY is read from .env (via python-dotenv) or from the
environment.  It is never printed or logged.
"""

import base64
import os
import sys

from dotenv import load_dotenv
from sarvamai import SarvamAI

load_dotenv()  # picks up .env in the project root

api_key = os.environ.get("SARVAM_API_KEY", "")
if not api_key:
    sys.exit("ERROR: SARVAM_API_KEY is not set.  Add it to .env and retry.")

client = SarvamAI(api_subscription_key=api_key)

# ── 1. Text-to-Speech ─────────────────────────────────────────────────────────
TTS_TEXT = "नमस्ते! मैं EchoMint हूँ, आपका डिजिटल बैंकिंग सहायक।"
TTS_OUT  = "sarvam_tts_out.wav"

print("[1/2] Calling Sarvam TTS (bulbul:v4-flash) ...")
tts_response = client.text_to_speech.convert(
    text=TTS_TEXT,
    model="bulbul:v4-flash",
    language_code="hi-IN",
    speaker="shubh_enhi_ads",   # default v4-flash speaker
    speech_sample_rate=16000,
)

# Response.audios is a list of base64-encoded WAV strings; join and decode.
wav_bytes = base64.b64decode("".join(tts_response.audios))
with open(TTS_OUT, "wb") as f:
    f.write(wav_bytes)
print(f"  OK  Audio saved to {TTS_OUT} ({len(wav_bytes):,} bytes)")

# ── 2. Speech-to-Text ─────────────────────────────────────────────────────────
print(f"\n[2/2] Calling Sarvam STT (saaras:v4) on {TTS_OUT} ...")
with open(TTS_OUT, "rb") as audio_file:
    stt_response = client.speech_to_text.transcribe(
        file=audio_file,
        model="saaras:v4",
        mode="transcribe",
    )

print(f"  OK  Transcript : {stt_response.transcript}")
print(f"      Language   : {stt_response.language_code}")

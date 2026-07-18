"""
Voice Enrollment Script
--------------------------
Run this ONCE to record your voice and save your speaker embedding to disk.
The banking agent will use this saved profile to verify it's really you
before starting each session.

INSTALL:
    uv add speechbrain torch soundfile sounddevice numpy

USAGE:
    python enroll_voice.py
"""

import sounddevice as sd
import soundfile as sf
import numpy as np
import torch
import torchaudio
from speechbrain.inference.speaker import EncoderClassifier
from speechbrain.utils.fetching import LocalStrategy

SAMPLE_RATE = 16000
PROFILE_PATH = "voice_profile.npy"


def load_wav_as_tensor(path):
    """Load a wav file and return a mono, 16kHz torch tensor,
    without relying on speechbrain's load_audio (which has a buggy
    lazy import of an unrelated 'k2' dependency)."""
    audio, sample_rate = sf.read(path)
    if audio.ndim > 1:  # convert stereo to mono if needed
        audio = audio.mean(axis=1)

    signal = torch.tensor(audio, dtype=torch.float32)

    if sample_rate != SAMPLE_RATE:
        resampler = torchaudio.transforms.Resample(orig_freq=sample_rate, new_freq=SAMPLE_RATE)
        signal = resampler(signal)

    return signal


def record_audio(filename, duration=5):
    print(f"\nRecording for {duration} seconds... Speak naturally!")
    audio = sd.rec(int(duration * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1)
    sd.wait()
    sf.write(filename, audio, SAMPLE_RATE)


def main():
    print("Loading speaker embedding model...")
    classifier = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir="pretrained_models/spkrec-ecapa-voxceleb",
        local_strategy=LocalStrategy.COPY,
    )

    input("Press Enter, then speak for 5 seconds to enroll your voice...")
    record_audio("enroll_sample.wav", duration=5)

    print("Extracting embedding...")
    signal = load_wav_as_tensor("enroll_sample.wav")
    embedding = classifier.encode_batch(signal.unsqueeze(0))
    embedding = embedding.squeeze().detach().numpy()

    np.save(PROFILE_PATH, embedding)
    print(f"\nVoice profile saved to '{PROFILE_PATH}'.")
    print("You can now run the banking agent — it will verify your voice against this profile.")


if __name__ == "__main__":
    main()
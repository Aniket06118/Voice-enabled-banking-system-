"""
Simple Voice Embedding Test Script (SpeechBrain version)
-----------------------------------------------------------
Records audio from your microphone, extracts a voice embedding using
SpeechBrain's ECAPA-TDNN speaker model, and compares two recordings to
see how similar the voices are.

INSTALL (run in terminal first):
    uv add speechbrain torch soundfile sounddevice numpy

USAGE:
    python voice_embedding_test.py

NOTE: The first run will download the pretrained model (~80MB) from
Hugging Face and cache it locally, so it needs internet access once.
"""

import sounddevice as sd
import soundfile as sf
import numpy as np
import torch
from speechbrain.inference.speaker import EncoderClassifier

SAMPLE_RATE = 16000  # required by the model


def record_audio(filename, duration=5):
    print(f"\nRecording '{filename}' for {duration} seconds... Speak now!")
    audio = sd.rec(int(duration * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1)
    sd.wait()
    sf.write(filename, audio, SAMPLE_RATE)
    print(f"Saved to {filename}")


def get_embedding(classifier, filename):
    signal = classifier.load_audio(filename)
    embedding = classifier.encode_batch(signal.unsqueeze(0))
    return embedding.squeeze().detach().numpy()


def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))


def main():
    print("Loading pretrained speaker embedding model (first run downloads it)...")
    classifier = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir="pretrained_models/spkrec-ecapa-voxceleb",
    )

    input("Press Enter, then speak for 5 seconds (Sample 1)...")
    record_audio("sample1.wav", duration=5)

    input("\nPress Enter, then speak again for 5 seconds (Sample 2)...")
    record_audio("sample2.wav", duration=5)

    print("\nExtracting embeddings...")
    emb1 = get_embedding(classifier, "sample1.wav")
    emb2 = get_embedding(classifier, "sample2.wav")

    print(f"Embedding shape: {emb1.shape}")  # e.g. (192,)

    similarity = cosine_similarity(emb1, emb2)
    print(f"\nCosine similarity between the two recordings: {similarity:.4f}")

    if similarity > 0.75:
        print("=> Likely the SAME speaker.")
    else:
        print("=> Likely DIFFERENT speakers (or too much noise/short audio).")


if __name__ == "__main__":
    main()
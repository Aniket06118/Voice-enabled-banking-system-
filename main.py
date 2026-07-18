from langchain.agents import create_agent
from agent_tools import tool_box
from dotenv import load_dotenv
import speech_recognition as sr
import pyttsx3
import numpy as np
import os
import soundfile as sf
import torch
import torchaudio
from speechbrain.inference.speaker import EncoderClassifier
from speechbrain.utils.fetching import LocalStrategy

load_dotenv(override=True)


SYSTEM_PROMPT = """
You are a banking assistant. Your job is to understand the user's requests and call the correct tool: make_payment for sending money, or get_payment_history for questions about past payments.

Rules:
1. If the user wants to make a payment and clearly states both a recipient and an amount, call make_payment immediately with those values.
2. If the recipient or amount is missing or unclear for a payment, do NOT call make_payment. Instead, ask the user a short, direct question to get the missing piece (e.g. "How much would you like to send to Rahul?").
3. Never calculate, guess, or assume an amount or recipient that the user did not clearly state.
4. Never make up account balances, transaction results, or confirmations yourself. The make_payment tool is the only source of truth — always call it before telling the user a payment succeeded or failed.
5. If the user asks anything about past payments, spending, or history (e.g. "how much did I send Rahul?", "what was my last payment?", "total spent this week?"), call get_payment_history to get the data, then answer the question yourself based on what it returns. Do not guess or make up numbers — if the data doesn't contain what's needed to answer, say so.
6. After a tool responds, relay the result back to the user in one short, natural sentence (or a brief summary for history questions). Do not add extra details the tool didn't provide.
7. If a tool reports an error (insufficient balance, recipient not found, etc.), clearly tell the user what went wrong. Do not retry the payment yourself or suggest workarounds.
8. You do not have access to the user's balance, contacts, or transaction history directly — you only know what a tool tells you when you call it. Never answer from memory or assumption.
9. Keep responses brief and conversational, since they will be spoken aloud (text-to-speech).

"""

agent = create_agent(model="google_genai:gemini-2.5-flash",
                      system_prompt=SYSTEM_PROMPT,
                      tools=tool_box)

# --- Voice setup ---
recognizer = sr.Recognizer()

PROFILE_PATH = "voice_profile.npy"
SIMILARITY_THRESHOLD = 0.65
MODEL_SAMPLE_RATE = 16000


def load_wav_as_tensor(path):
    """Load a wav file and return a mono, 16kHz torch tensor,
    without relying on speechbrain's load_audio (which has a buggy
    lazy import of an unrelated 'k2' dependency)."""
    audio, sample_rate = sf.read(path)
    if audio.ndim > 1:  # convert stereo to mono if needed
        audio = audio.mean(axis=1)

    signal = torch.tensor(audio, dtype=torch.float32)

    if sample_rate != MODEL_SAMPLE_RATE:
        resampler = torchaudio.transforms.Resample(orig_freq=sample_rate, new_freq=MODEL_SAMPLE_RATE)
        signal = resampler(signal)

    return signal


def extract_text(content):
    """Pull plain text out of LangChain message content, which can be
    a plain string or a list of content blocks (dicts with 'type'/'text')."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return " ".join(parts).strip()
    return str(content)


def speak(text):
    """Convert text to speech and play it."""
    print(text)
    engine = pyttsx3.init()  # fresh instance each call avoids a pyttsx3/Windows bug
    engine.say(text)
    engine.runAndWait()
    engine.stop()


def listen(save_path=None):
    """Record from mic and convert speech to text. If save_path is given,
    also saves the raw audio to that .wav file (used for voice verification)."""
    with sr.Microphone() as source:
        print("Listening...")
        recognizer.adjust_for_ambient_noise(source, duration=0.5)
        audio = recognizer.listen(source)

    if save_path:
        with open(save_path, "wb") as f:
            f.write(audio.get_wav_data())

    try:
        text = recognizer.recognize_google(audio)
        print(f"You said: {text}")
        return text
    except sr.UnknownValueError:
        print("Sorry, I didn't catch that.")
        return None
    except sr.RequestError as e:
        print(f"STT error: {e}")
        return None


def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))


def verify_voice_from_file(classifier, wav_path):
    """Compare a recorded wav file against the saved voice profile.
    Returns True if it's likely the enrolled user, False otherwise."""
    if not os.path.exists(PROFILE_PATH):
        speak("No enrolled voice profile found. Please run enroll_voice.py first.")
        return False

    enrolled_embedding = np.load(PROFILE_PATH)

    signal = load_wav_as_tensor(wav_path)
    embedding = classifier.encode_batch(signal.unsqueeze(0))
    embedding = embedding.squeeze().detach().numpy()

    similarity = cosine_similarity(enrolled_embedding, embedding)
    print(f"Voice similarity: {similarity:.4f}")

    return similarity >= SIMILARITY_THRESHOLD


def main():
    print("Loading speaker verification model...")
    classifier = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir="pretrained_models/spkrec-ecapa-voxceleb",
        local_strategy=LocalStrategy.COPY,
    )

    conversation = []  # keeps full chat history across turns

    print("Voice Banking Assistant (say 'exit' to quit)")

    # First turn doubles as the voice verification sample
    speak("Please say your request to begin.")
    first_input = listen(save_path="verify_sample.wav")

    if first_input is None:
        speak("Sorry, I didn't catch that. Please try again.")
        return

    if not verify_voice_from_file(classifier, "verify_sample.wav"):
        speak("Voice verification failed. This does not match the enrolled user. Stopping.")
        return

    # Verified — process their first request normally
    conversation.append({"role": "user", "content": first_input})
    result = agent.invoke({"messages": conversation})
    reply = result["messages"][-1]
    speak(extract_text(reply.content))
    conversation = result["messages"]

    while True:
        user_input = listen()
        if user_input is None:
            continue  # nothing understood, listen again

        if user_input.lower() in {"exit", "quit"}:
            speak("Goodbye!")
            break

        conversation.append({"role": "user", "content": user_input})

        result = agent.invoke({"messages": conversation})

        # result["messages"] is the full updated history (including tool calls)
        reply = result["messages"][-1]
        speak(extract_text(reply.content))

        # keep the full trace so the agent remembers earlier turns
        conversation = result["messages"]


if __name__ == "__main__":
    main()
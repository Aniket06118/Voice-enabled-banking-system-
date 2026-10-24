<div align="center">

# 🏦 EchoMint

### A Voice-Enabled Banking System for the Elderly

*Pay someone, check your balance, or hear what you spent — just by talking.*

[![Python](https://img.shields.io/badge/Python-3.13+-3776AB?logo=python&logoColor=white)](https://python.org)
[![Django](https://img.shields.io/badge/Django-5.2-092E20?logo=django&logoColor=white)](https://djangoproject.com)
[![Gemini](https://img.shields.io/badge/Gemini_AI-Agent-4285F4?logo=google&logoColor=white)](https://ai.google.dev)
[![Sarvam](https://img.shields.io/badge/Sarvam_AI-11_Languages-FF6B00)](https://sarvam.ai)
[![PostgreSQL](https://img.shields.io/badge/Neon_Postgres-Serverless-4169E1?logo=postgresql&logoColor=white)](https://neon.tech)
[![Deploy](https://img.shields.io/badge/Deploy_on-Render-46E3B7?logo=render&logoColor=white)](https://render.com)

</div>

---

## 📋 Table of Contents

- [Overview](#-overview)
- [Key Features](#-key-features)
- [Architecture](#-architecture)
- [Tech Stack](#-tech-stack)
- [Getting Started](#-getting-started)
- [Environment Variables](#-environment-variables)
- [API Reference](#-api-reference)
- [Voice Security](#-voice-security)
- [AI Assistant](#-ai-assistant)
- [Spending Analytics](#-spending-analytics)
- [Deployment](#-deployment)
- [Management Commands](#-management-commands)
- [Testing](#-testing)
- [Live Demo Checklist](#-live-demo-checklist)
- [Project Structure](#-project-structure)
- [Contributing](#-contributing)
- [License](#-license)

---

## 🌟 Overview

**EchoMint** is a full-stack, voice-first banking web application designed with the elderly in mind. It combines **biometric voice authentication** (speaker verification + liveness detection), an **AI-powered conversational agent** (Gemini), and **multilingual speech** (11 Indian languages via Sarvam AI) into a Django-powered platform with a Neon PostgreSQL backend.

The core philosophy: **every banking action that can be spoken should be spoken**, while security is never compromised — your voice unlocks the app, small payments need a spoken "yes", and larger ones still require a typed PIN that never touches the AI.

---

## ✨ Key Features

### 🎙️ Voice-First Banking
- **Talk to your bank** — send money, pay bills, recharge mobiles, buy digital gold, check balances, and review history, all by voice
- **11 Indian languages** — English, Hindi, Tamil, Telugu, Bengali, Marathi, Gujarati, Kannada, Malayalam, Punjabi, and Odia
- **Instant answers** — common requests (balance, yes/no, "send ₹300 to Aman") are answered in ~50ms from the database, no AI round trip needed
- **AI fallback** — anything the instant engine can't handle goes to the Gemini agent with live account context

### 🔐 Voice Biometric Security
- **Speaker verification** — ECAPA-TDNN voiceprint via SpeechBrain (never stores raw audio, only a numerical embedding)
- **Liveness detection** — random digits that change every 15 seconds, transcribed server-side; a recording of an old login can't pass
- **Personal thresholds** — each user gets a match line fitted to their own voice (0.32–0.42), not a one-size-fits-all cutoff
- **Anti-spoofing** — compares every login against ALL enrolled voices; if you sound equally like two people, access is denied

### 💸 Secure Payments
- **Two-step flow** — `initiate` → `confirm`, so users always get a chance to review
- **Under ₹500** — voice confirmation ("yes" / "haan")
- **₹500 and above** — 4-digit PIN on screen (UPI-style), never spoken, never touches the AI
- **Idempotency** — same key twice = same payment, never double-charged
- **Daily limits** — configurable per-account spending cap
- **PIN lockout** — 3 wrong PINs → 15-minute cooldown

### 📊 Spending Analytics
- **Auto-categorisation** — rule-based (food, travel, shopping, bills, health, groceries, entertainment, transfers)
- **Interactive dashboard** — donut charts, bar charts, trend lines, calendar heatmaps, weekday patterns, period comparisons
- **All numbers from the database** — `Sum`, `Count`, `Avg`, `Max`, `Trunc` — never computed by an LLM
- **Ask the assistant** — "analyse my food spending last month" generates a sharable analytics page

### 🌐 Full Web Application
- **Landing page** — interactive 3D "how a payment works" machine (Three.js, bundled offline)
- **Sign up / Log in / Forgot password** — complete auth flow with animated UI
- **Banking dashboard** — balance, contacts, payment history, notifications, card freeze
- **Admin panel** — Django admin with live payments, accounts, voice profiles, and audit logs

---

## 🏗️ Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                        Browser (UI)                           │
│  Landing │ Login │ Banking App │ Analytics │ Voice Enrollment  │
│  Three.js 3D │ Robot animation │ Mic capture │ Chart.js        │
└────────────────────┬──────────────────────────────────────────┘
                     │ JSON API / Audio uploads
┌────────────────────▼──────────────────────────────────────────┐
│                    Django Backend                              │
│                                                               │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────┐    │
│  │  views.py   │  │  intents.py  │  │    agent.py        │    │
│  │  (API layer)│→ │  (instant    │→ │  (Gemini LangChain │    │
│  │             │  │   answers)   │  │   agent, tools)    │    │
│  └──────┬──────┘  └──────────────┘  └────────┬──────────┘    │
│         │                                     │               │
│  ┌──────▼─────────────────────────────────────▼──────────┐   │
│  │                   services.py                          │   │
│  │        Single source of truth for all money moves      │   │
│  │   initiate_payment │ confirm_payment │ verify_pin      │   │
│  └──────────────────────────┬────────────────────────────┘   │
│                             │                                 │
│  ┌──────────────┐  ┌───────▼────────┐  ┌──────────────────┐  │
│  │   voice.py   │  │   models.py    │  │  analysis.py     │  │
│  │  Speaker     │  │  Account, PIN, │  │  Charts, trends, │  │
│  │  verification│  │  Payment, Audit│  │  aggregates      │  │
│  └──────────────┘  └───────┬────────┘  └──────────────────┘  │
└────────────────────────────┼─────────────────────────────────┘
                             │
              ┌──────────────▼──────────────┐
              │    Neon PostgreSQL (cloud)    │
              │    SQLite fallback (local)    │
              └──────────────────────────────┘

External services:
  • Gemini AI  — conversational agent + digit transcription
  • Sarvam AI  — multilingual STT (saaras:v4) + TTS (bulbul:v4-flash)
  • SpeechBrain — ECAPA-TDNN speaker embedding (runs locally)
```

---

## 🛠️ Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Python 3.13 · Django 5.2 · Gunicorn |
| **Database** | Neon PostgreSQL (serverless) · SQLite fallback |
| **AI Agent** | Gemini 3.1 Flash Lite · LangChain |
| **Voice Biometrics** | SpeechBrain ECAPA-TDNN · PyTorch |
| **Multilingual Speech** | Sarvam AI (saaras:v4 STT + bulbul:v4-flash TTS) |
| **Frontend** | Django templates · Vanilla JS · CSS · Three.js (3D) |
| **Static Files** | WhiteNoise (production) |
| **Deployment** | Render · Gunicorn |

---

## 🚀 Getting Started

### Prerequisites

- Python 3.13+
- A [Neon](https://neon.tech) PostgreSQL database (or use the built-in SQLite fallback)
- A [Gemini API key](https://aistudio.google.com/apikey) (free)
- *(Optional)* A [Sarvam AI key](https://dashboard.sarvam.ai) for 11-language speech

### Installation

```bash
# 1. Clone the repo
git clone https://github.com/Aniket06118/Voice-enabled-banking-system-.git
cd Voice-enabled-banking-system-

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
copy .env.example .env          # Windows
# cp .env.example .env          # macOS / Linux
# Then edit .env with your keys (see Environment Variables below)

# 5. Run database migrations
python manage.py migrate

# 6. Seed the demo account
python manage.py seed_demo
# Creates: phone 9876543210 / password demo1234 / PIN 1234
# Adds ~90 realistic payments over 6 months, contacts, and billers

# 7. (Optional) Create an admin superuser
python manage.py createsuperuser

# 8. Start the development server
python manage.py runserver
```

### Verify It Works

| URL | What You Should See |
|-----|-------------------|
| http://127.0.0.1:8000/api/health/ | `{"ok": true, "database": "postgresql"}` |
| http://127.0.0.1:8000/ | Landing page with 3D payment machine |
| http://127.0.0.1:8000/login/ | Log in (demo: `9876543210` / `demo1234`) |
| http://127.0.0.1:8000/app/ | The banking app (tap the mic or type) |
| http://127.0.0.1:8000/admin/ | Django admin panel |

---

## 🔧 Environment Variables

Copy `.env.example` to `.env` and configure:

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | No | Neon Postgres connection string. Omit for SQLite fallback |
| `DJANGO_SECRET_KEY` | Yes | Any long random string |
| `DJANGO_DEBUG` | No | `1` for dev, `0` for production |
| `GOOGLE_API_KEY` | Yes | Gemini API key for the conversational agent |
| `AGENT_MODEL` | No | Model name (default: `gemini-3.1-flash-lite`) |
| `SARVAM_API_KEY` | No | Sarvam AI key for 11-language speech. Without it, browser speech is used |
| `VOICE_VERIFICATION_REQUIRED` | No | `1` (default) to require voice check; `0` to disable |
| `VOICE_SHOW_SCORES` | No | `1` to display voice match scores (great for demos) |
| `VOICE_THRESHOLD_CAP` | No | Strictest match cutoff (default: `0.45`) |
| `DEMO_PHONE` | No | Phone shown on login page (default: `9876543210`) |
| `DEMO_PASSWORD` | No | Password shown on login page (default: `demo1234`) |
| `GOLD_PRICE_PER_GRAM` | No | Digital gold price per gram, 24K (default: `14929`) |

---

## 📡 API Reference

All endpoints return JSON. Authentication is via Django sessions.

### Auth

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| `POST` | `/api/login/` | `{phone, password}` | Log in |
| `POST` | `/api/logout/` | — | Log out |
| `POST` | `/api/signup/` | `{full_name, phone, password, pin}` | Create account |
| `POST` | `/api/password/reset/` | `{phone, pin, password}` | Reset password via PIN |
| `GET` | `/api/csrf/` | — | Get CSRF cookie |

### Banking

| Method | Endpoint | Body / Query | Description |
|--------|----------|------|-------------|
| `GET` | `/api/me/` | — | Account details + contacts |
| `GET` | `/api/history/` | `?recipient=&days=&limit=` | Payment history |
| `GET` | `/api/summary/` | `?recipient=&days=` | Spending totals (DB-computed) |
| `GET` | `/api/notifications/` | — | Account notifications |
| `POST` | `/api/payments/` | `{recipient, amount, idempotency_key?, reference?}` | Start payment (PENDING) |
| `POST` | `/api/payments/<id>/confirm/` | `{pin?}` | Confirm (PIN if ≥ ₹500) |
| `POST` | `/api/payments/<id>/cancel/` | — | Cancel pending payment |
| `POST` | `/api/pin/change/` | `{old_pin, new_pin}` | Change PIN |
| `POST` | `/api/settings/daily-limit/` | `{daily_limit, pin}` | Set daily spending limit |
| `POST` | `/api/card/freeze/` | `{frozen}` | Freeze/unfreeze virtual card |
| `POST` | `/api/contacts/` | `{name, upi_id}` | Add a contact |
| `POST` | `/api/contacts/<id>/remove/` | — | Remove a contact |

### Voice Security

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/voice/status/` | Enrollment & verification status |
| `GET` | `/api/voice/challenge/` | Get random digits to read aloud |
| `POST` | `/api/voice/enroll/` | Save voiceprint (3 recordings) |
| `POST` | `/api/voice/verify/` | Verify speaker + liveness |
| `POST` | `/api/voice/fallback/` | PIN fallback when voice fails |
| `POST` | `/api/voice/lock/` | Re-lock the session |

### Speech (Sarvam AI)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/voice/stt/` | Speech → text (multilingual) |
| `POST` | `/api/voice/tts/` | Text → speech (MP3 reply) |

### AI Agent

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| `POST` | `/api/agent/chat/` | `{message, lang?}` | One turn with the banking agent |
| `POST` | `/api/agent/reset/` | — | Clear conversation history |

### Analytics

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/analysis/` | Live analysis (`?category=&period=`) |
| `GET` | `/api/analysis/<id>/` | Saved analysis by ID |

---

## 🔐 Voice Security

EchoMint's voice security runs two independent checks in parallel:

### 1. Speaker Verification
An **ECAPA-TDNN** embedding (SpeechBrain) is compared against the enrolled voiceprint. Each user has a **personal match threshold** fitted to their own voice at enrollment (range: 0.32–0.42), not a global cutoff.

### 2. Liveness Detection
Random digits are displayed and change every **15 seconds**. The user reads them aloud, and the audio is transcribed **server-side** (Gemini + Sarvam, in parallel — first correct answer wins). A replayed recording from a previous session can't contain digits that didn't exist yet.

### Security Properties
| Property | How |
|----------|-----|
| **Replay protection** | Fresh random digits every 15s, single-use |
| **Cross-account protection** | Every login is compared against ALL enrolled voices |
| **Brute-force protection** | 5 failed voice attempts → 2-minute pause |
| **Graceful fallback** | PIN bypass if room is noisy, mic fails, or speech services are down |
| **Voiceprint tampering** | Re-recording voice requires PIN or a live voice match |
| **Privacy** | Only numerical embeddings are stored, never raw audio |

### Enrollment Flow
1. Read **3 random 8-digit sequences** (~30 seconds total)
2. Server trims silence, normalises loudness, extracts embeddings
3. Consistency check ensures all 3 clips are from the same person
4. Personal threshold is fitted; voiceprint is saved

### Login Flow
1. Random 6–8 digits appear with a 15-second countdown
2. Microphone auto-stops when you stop talking
3. Speaker check + digit transcription run in parallel
4. One misheard digit is forgiven if the voice is clearly the owner's

> **Emergency off-switch:** Set `VOICE_VERIFICATION_REQUIRED=0` in `.env`

> **Honest limit:** This stops replayed recordings and other people's voices. A high-quality real-time AI voice clone could still fool voice alone — which is why ₹500+ payments always require the typed PIN.

---

## 🤖 AI Assistant

The conversational agent ("EchoMint") combines two layers:

### Fast Layer — `intents.py` (~50ms)
Handles the most common requests without any AI call:
- ✅ / ❌ confirm or cancel a waiting payment
- 💰 "What's my balance?"
- 💸 "Send ₹300 to Aman"
- 🔌 "Pay my electricity bill ₹1,200"
- ⚙️ "Open settings" / "सेटिंग्स खोलो"
- 📊 "Analyse my food spending last month"
- 📜 "Show my last payments"

Works in **English and Hindi** (including Devanagari numerals).

### AI Layer — `agent.py` (1–4 seconds)
Everything else goes to **Gemini** via LangChain, with:
- Live account facts injected in the system prompt (balance, contacts, pending payments)
- Tool calls for payments, history, summaries, navigation, settings, and analytics
- Conversation memory (last 8 turns)
- Automatic fallback to a backup model if the primary is overloaded

### Supported Languages
| Language | Speech Engine |
|----------|--------------|
| English | Browser (streaming) or Sarvam |
| Hindi, Tamil, Telugu, Bengali, Marathi, Gujarati, Kannada, Malayalam, Punjabi, Odia | Sarvam AI (saaras:v4 STT + bulbul:v4-flash TTS) |

---

## 📊 Spending Analytics

Payments are **auto-categorised** by rule-based keyword matching (no LLM):

| Category | Example Matches |
|----------|----------------|
| 🍕 Food | Swiggy, Zomato, Domino's, Starbucks |
| 🚗 Travel | Uber, Ola, IRCTC, Petrol |
| 🛍️ Shopping | Amazon, Flipkart, Myntra |
| 🧾 Bills | Electricity, Broadband, Insurance, Recharge |
| 🎬 Entertainment | Netflix, Hotstar, BookMyShow |
| 💊 Health | Apollo, 1mg, Practo |
| 🥬 Groceries | BigBasket, Blinkit, Zepto |
| 💸 Transfers | Person-to-person payments |

The analytics dashboard generates:
- **Donut charts** — category breakdown
- **Bar / stacked bar charts** — monthly trends
- **Line / area charts** — spending over time
- **Calendar heatmaps** — daily spending intensity
- **Weekday patterns** — which days you spend most
- **Period comparisons** — this month vs. last month

Ask the assistant: *"Analyse my food spending last month"* → generates a sharable analytics page.

---

## 🚀 Deployment

### Deploy to Render (Recommended)

The repo includes a `render.yaml` blueprint and `build.sh` script.

1. **Push to GitHub** (already done)
2. Go to [dashboard.render.com](https://dashboard.render.com)
3. Click **New +** → **Blueprint** → connect your GitHub repo
4. Set environment variables in the dashboard:
   - `DATABASE_URL` — your Neon connection string
   - `GOOGLE_API_KEY` — your Gemini key
   - `SARVAM_API_KEY` — your Sarvam key
5. Click **Create Web Service** → Render runs `build.sh` and starts `gunicorn`

> ⚠️ **Note:** `torch` + `speechbrain` are ~2 GB. If Render's free tier (512 MB RAM) can't handle it, either upgrade to the Starter plan ($7/mo) or set `VOICE_VERIFICATION_REQUIRED=0` to skip voice checks.

### Production Settings (already configured)
- **Gunicorn** — production WSGI server
- **WhiteNoise** — serves static files with Brotli compression
- **`SECURE_PROXY_SSL_HEADER`** — trusts Render's HTTPS proxy
- **`CSRF_TRUSTED_ORIGINS`** — allows `.onrender.com` forms

---

## 🔨 Management Commands

```bash
# Seed the demo account (phone: 9876543210 / password: demo1234 / PIN: 1234)
# Adds ~90 realistic payments, contacts, and billers
python manage.py seed_demo

# Re-categorise old payments after updating category rules
python manage.py backfill_categories

# Voice profile report — shows voiceprint similarity matrix
python manage.py voice_report

# Reset a user's voiceprint (re-enrollment required)
python manage.py voice_report --reset <phone>

# Unlock a locked-out voice profile
python manage.py voice_report --unlock <phone>

# Django admin superuser
python manage.py createsuperuser
```

---

## 🧪 Testing

```bash
# Run the full test suite
python manage.py test bank

# Tests use:
#   - MD5 password hasher (fast)
#   - Mocked speech engines (no real API calls)
#   - In-memory database operations
```

The test suite (`bank/tests.py`, ~78 KB) covers:
- Payment flows (initiate, confirm, cancel, PIN lockout, daily limits, idempotency)
- Voice enrollment and verification (mocked embeddings)
- Voice security edge cases (replay, cross-account, lockout, fallback)
- AI agent tool calls
- Intent matching (English + Hindi)
- Spending analysis and categorisation
- API authentication and authorisation
- Account creation and password reset

---

## 🎤 Live Demo Checklist

Preparing for a live demo or hackathon judging? Follow these steps:

1. **Record voices** — Each person records their voice on **their own account only**, in the demo room, on the demo mic:
   ```bash
   python manage.py voice_report --reset <phone>
   ```
   Then each person enrolls via the app.

2. **Verify separation** — The two voices should show as "clearly different" (similarity < 0.35):
   ```bash
   python manage.py voice_report
   ```

3. **Enable score display** — In `.env`:
   ```
   VOICE_SHOW_SCORES=1
   SARVAM_API_KEY=<your-key>
   ```
   Restart the server (the voice model loads at startup).

4. **On stage:**
   - Owner reads the number → ✅ Verified
   - Click **Lock** (bottom-left of the app)
   - Friend reads the number → ❌ "Access denied"
   - Owner taps **Try again** → ✅ Verified

5. **Locked out?**
   ```bash
   python manage.py voice_report --unlock <phone>
   ```

---

## 📂 Project Structure

```
Voice-enabled-banking-system-/
├── config/                     # Django project settings
│   ├── settings.py             # All configuration (DB, voice, AI, security)
│   ├── urls.py                 # Root URL routing
│   └── wsgi.py                 # WSGI entry point (Gunicorn)
│
├── bank/                       # Main Django app
│   ├── models.py               # Account, Contact, Payment, VoiceProfile, AuditLog, Analysis
│   ├── services.py             # All banking rules (single source of truth for money)
│   ├── views.py                # JSON API + page views
│   ├── urls.py                 # API URL routing
│   ├── agent.py                # Gemini LangChain agent with banking tools
│   ├── intents.py              # Instant answers (no AI, ~50ms) for common requests
│   ├── voice.py                # Speaker verification (ECAPA-TDNN) + liveness detection
│   ├── analysis.py             # Spending analytics dashboard builder
│   ├── categories.py           # Rule-based payment categorisation
│   ├── sms.py                  # OTP via Twilio / 2Factor.in / console
│   ├── middleware.py           # API error handling middleware
│   ├── admin.py                # Django admin configuration
│   ├── tests.py                # Comprehensive test suite (~78 KB)
│   ├── templates/bank/         # HTML templates
│   │   ├── landing.html        # Home page with 3D payment machine
│   │   ├── login.html          # Login with animated background
│   │   ├── signup.html         # Account creation
│   │   ├── forgot.html         # Password reset
│   │   ├── app.html            # The banking app (chat, mic, PIN pad)
│   │   └── analysis.html       # Spending analytics dashboard
│   ├── static/bank/            # Frontend assets
│   │   ├── site.css            # Styles
│   │   ├── bank-machine.js     # 3D Three.js payment machine
│   │   ├── robot.js            # Animated robot (listening/thinking/speaking)
│   │   ├── fonts/              # Bundled fonts (work offline)
│   │   └── vendor/             # Three.js (bundled, no CDN needed)
│   └── management/commands/    # CLI tools
│       ├── seed_demo.py        # Generate demo account + realistic payments
│       ├── backfill_categories.py  # Re-categorise existing payments
│       └── voice_report.py     # Voice profile diagnostics & management
│
├── manage.py                   # Django CLI entry point
├── requirements.txt            # Python dependencies
├── pyproject.toml              # Project metadata
├── build.sh                    # Render build script
├── render.yaml                 # Render deployment blueprint
├── .env.example                # Environment variable template
└── .gitignore
```

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📄 License

This project is open source and available under the [MIT License](LICENSE).

---

<div align="center">

**Built with ❤️ by [Aniket](https://github.com/Aniket06118)**

*EchoMint — because banking should be as easy as talking.*

</div>

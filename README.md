# EchoMint Web (Django backend)

Voice-enabled banking. This is the Django side: database, banking rules, API.
The agent (`bank/agent.py`) and the website (`/`) both use `bank/services.py`, the single place money moves.

## Rules decided by the team
- Payment **under ₹500** -> user confirms by voice ("yes").
- Payment **₹500 and above** -> user must enter their **4-digit PIN on screen** (like UPI).
  The PIN is never spoken. 3 wrong PINs lock PIN payments for 15 minutes.
- Every payment is 2 steps: `initiate` (PENDING) -> `confirm` (money moves, atomic).
- Same `idempotency_key` twice = same payment, never charged twice.

## Setup (first time)
```
python -m venv .venv
.venv\Scripts\activate          # Windows   (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # Mac/Linux: cp .env.example .env   then paste your Neon URL into .env
python manage.py migrate        # creates all tables in Neon
python manage.py seed_demo      # demo user: phone 9876543210 / password demo1234 / PIN 1234
python manage.py createsuperuser   # your admin login for /admin/
python manage.py runserver
```
Check it works: open http://127.0.0.1:8000/api/health/  -> `{"ok": true, "database": "postgresql"}`
Admin panel (live payments, accounts, audit log): http://127.0.0.1:8000/admin/

Run the tests any time: `python manage.py test bank`

## API (all JSON)
| Method | URL | What |
|---|---|---|
| POST | /api/login/ `{phone, password}` | log in |
| GET | /api/me/ | account details + contacts |
| POST | /api/payments/ `{recipient, amount, idempotency_key?}` | start a payment (PENDING) |
| POST | /api/payments/<id>/confirm/ `{pin?}` | confirm (pin needed if >= 500) |
| POST | /api/payments/<id>/cancel/ | cancel |
| GET | /api/history/?recipient=&days=&limit= | past payments |
| GET | /api/summary/?recipient=&days= | exact totals computed by the DB |

## Web app
- http://127.0.0.1:8000/signup/  open a new account (gets ₹10,000 demo money and the usual billers)
- http://127.0.0.1:8000/analysis/  spending analytics (also opens from the assistant: "analyse my food spending last month")
- http://127.0.0.1:8000/        home page with the 3D "how a payment works" machine
- http://127.0.0.1:8000/login/  log in (animated line background); demo: 9876543210 / demo1234
- http://127.0.0.1:8000/app/    the bank itself: tap the mic (Chrome/Edge) or type
three.js is bundled in bank/static/bank/vendor, so the 3D works without internet (fonts fall back to system fonts offline).
- Under 500: the agent asks "Confirm?" -> say "yes".
- 500 and above: a PIN pad appears (demo PIN 1234). The PIN goes straight to Django, never through the AI.
- If the AI is slow or down, the user gets a calm spoken fallback instead of silence.

## Voice security
After the password login, the user must pass a voice check before the agent or any payment endpoint works.
1. **First time (about 30 s):** read 3 random 8-digit sequences. The server cleans the audio (trims silence,
   evens loudness), saves a voiceprint (numbers only, never the audio) and fits a **personal match line** to that
   person's own enrollment (strictest 0.42, never below 0.32).
2. **Every login:** read 6 random digits. The recording stops by itself when you stop talking. The server checks
   (a) the voiceprint matches and (b) the digits spoken are the ones it just chose (transcribed server-side by
   Gemini, with a backup model). A recording of an old login can't contain digits that didn't exist yet.
   Both checks run at the same time, so there is little waiting. One misheard digit is forgiven only when the
   voice is clearly the owner's.
3. **Stays unlocked while you use the app** (30 min idle / 12 h max). A page refresh never asks again.
4. **It keeps learning:** very clean logins nudge the saved voiceprint toward the person's real mic and voice.
5. **Never stuck:** "Use my PIN instead" unlocks the app if the room is noisy, the mic misbehaves or Gemini is down
   (same 3-wrong-PINs lockout as payments). "Re-record my voice" needs the PIN or a live voice check, so a stolen
   password alone can't swap the voiceprint. 5 voice failures pause voice login for 2 minutes.
6. Users never see a score. For tuning, add `#debug` to the page address to see it, and read the audit log
   (`voice_verified` / `voice_failed` rows have the similarity and the threshold).

Needs: `pip install torch torchaudio speechbrain` (first run downloads the ~90 MB voice model).
Emergency off-switch: `VOICE_VERIFICATION_REQUIRED=0` in `.env`.
Honest limit: this stops replayed recordings and other people's voices; a high-quality *real-time* AI voice clone
could still fool voice alone, which is why money still needs the PIN from Rs 500 and why a PIN backup exists.

## Voice assistant: fast, multilingual, controls the whole site
- **Instant answers (no AI call, ~0.05 s):** yes/no to a waiting payment, balance, "send 300 to Aman",
  "pay my electricity bill 1200", "open settings", "show my last payments", "analyse my food spending last month",
  in English and Hindi (bank/intents.py). Everything else goes to Gemini (bank/agent.py) with live account facts
  in the prompt and "thinking" off, so most answers need a single model call.
- **11 languages (Aniket's Sarvam work):** English uses the browser's streaming recogniser (fastest); Indian languages
  use Sarvam saaras:v4 (speech-to-text, with your contact names as hints) and bulbul:v4-flash (voice, MP3).
  Settings → Speech engine lets you force Sarvam or the browser. No SARVAM_API_KEY = browser speech only.
- **Voice navigation:** the assistant can open any page or form, change language, text size, spoken replies, freeze
  the card, download the statement and log out. "Hands-free" keeps listening after each reply.
- **Robot:** the robot above the chat shows what the assistant is doing (listening, thinking, speaking, happy).

## Analytics (Aniket's spec)
Payments get a category automatically (bank/categories.py, rule-based). bank/analysis.py builds a dashboard spec
from database aggregates only and picks charts by the data (donut, stacked bar, trend line/area, top payees,
weekday pattern, calendar heatmap, comparison with the previous period). `python manage.py backfill_categories`
re-categorises old payments. `seed_demo` adds ~90 realistic payments over 6 months (once).

## Forgot password

The log-in page has a **Forgot password?** link. The customer enters their registered
mobile number, their 4-digit PIN (the one used for payments of ₹500+) and a new password.
Wrong PINs count towards the normal PIN lockout (3 tries, then 15 minutes), and a wrong
number gets the same answer as a wrong PIN, so the page can't be used to guess PINs or
find out who has an account. Changing the password signs out every other session.

## Voice check (hands-free)

The voice box opens, the microphone stays on, and a random number appears with a
15-second timer. Read it out: the clip is sent the moment you stop talking. No speech in
15 s, or a wrong try, gives a new number, so someone imitating the voice only ever has
15 seconds per number. The digits are checked by Sarvam and Gemini at the same time
(first correct answer wins), number words like "char saat" or "double five" count, and a
voice just under the line with the right digits is combined with the next try instead of
being rejected. Background noise never counts towards the 5-try lockout.

## Live voice demo: checklist

1. `python manage.py voice_report --reset <phone>` for every account, then each person records
   their voice on **their own account only**, in the demo room, on the demo microphone.
2. `python manage.py voice_report` must show the two voices as "ok, clearly different" (under 0.35).
3. `.env`: `VOICE_SHOW_SCORES=1`, `SARVAM_API_KEY` set. Restart the server (the voice model loads at start).
4. On stage: owner reads the number -> verified. Click **Lock** (bottom-left). Friend reads the number ->
   "Access denied" and listening pauses. Owner taps **Try again** -> verified.
5. Locked out after 5 failed checks? `python manage.py voice_report --unlock <phone>`.

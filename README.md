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

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
Open http://127.0.0.1:8000/ -> log in (demo: 9876543210 / demo1234) -> tap the mic (Chrome/Edge) or type.
- Under 500: the agent asks "Confirm?" -> say "yes".
- 500 and above: a PIN pad appears (demo PIN 1234). The PIN goes straight to Django, never through the AI.
- If the AI is slow or down, the user gets a calm spoken fallback instead of silence.

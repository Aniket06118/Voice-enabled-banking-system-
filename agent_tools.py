import json
from datetime import datetime, timezone
from typing import Optional
from langchain_core.tools import tool

USER_DATA_FILE = "user_data.json"
PAYMENT_HISTORY_FILE = "payment_history.json"


@tool
def make_payment(recipient_name: Optional[str] = None, amount: Optional[float] = None) -> str:
    """Make a payment to a person. Call this when the user wants to pay,
    send, or transfer money to someone.

    Args:
        recipient_name: Name of the person to pay (e.g. 'Rahul', 'Mom').
        amount: Amount of money to pay.
    """

    # 1. Check we have enough info
    if not recipient_name:
        return "I need to know who to pay. Please tell me the recipient's name."

    if amount is None:
        return "I need to know how much to pay. Please tell me the amount."

    if amount <= 0:
        return "The amount must be greater than zero."

    # 2. Load user data
    with open(USER_DATA_FILE, "r") as f:
        user_data = json.load(f)

    # 3. Check recipient exists
    contact = next(
        (c for c in user_data["contacts"] if c["name"].lower() == recipient_name.lower()),
        None,
    )
    if contact is None:
        return f"'{recipient_name}' was not found in your contacts."

    # 4. Check sufficient balance
    balance = user_data["user"]["balance"]
    if amount > balance:
        return f"Insufficient balance. Your current balance is {balance}."

    # 5. All checks passed -> deduct balance and record the payment
    new_balance = balance - amount
    user_data["user"]["balance"] = new_balance

    with open(USER_DATA_FILE, "w") as f:
        json.dump(user_data, f, indent=2)

    with open(PAYMENT_HISTORY_FILE, "r") as f:
        history = json.load(f)

    history.append({
        "recipient": contact["name"],
        "amount": amount,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "Success",
    })

    with open(PAYMENT_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

    return f"₹{amount} has been successfully transferred to {contact['name']}. Your remaining balance is ₹{new_balance}."


def load_history():
    try:
        with open(PAYMENT_HISTORY_FILE, "r") as f:
            content = f.read().strip()
            return json.loads(content) if content else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []
@tool
def get_payment_history() -> str:
    """Get the full payment history so you can analyze it and answer
    questions about past payments (e.g. totals, specific recipients,
    dates, largest/smallest payment, etc.).

    Use this whenever the user asks anything about past payments,
    spending, or transaction history.
    """
    history = load_history()  # reuse the same safe-load helper from before

    if not history:
        return "No payments have been made yet."

    return json.dumps(history, indent=2)


tool_box=[make_payment,get_payment_history]
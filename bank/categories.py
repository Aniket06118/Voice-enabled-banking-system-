"""Spending categories, assigned by simple rules (never by an LLM).

categorize("Swiggy")              -> "food"
categorize("Rahul Sharma", "person") -> "transfers"
"""
CATEGORIES = ["food", "travel", "shopping", "bills", "entertainment", "health",
              "groceries", "transfers", "other"]

LABELS = {"food": "Food", "travel": "Travel", "shopping": "Shopping", "bills": "Bills",
          "entertainment": "Entertainment", "health": "Health", "groceries": "Groceries",
          "transfers": "Transfers", "other": "Other"}

# One fixed colour per category (used by every chart).
COLORS = {"food": "#e8833a", "travel": "#2f56e8", "shopping": "#9b5de5", "bills": "#13244d",
          "entertainment": "#e5487a", "health": "#18a37a", "groceries": "#7fb83a",
          "transfers": "#5b8def", "other": "#9aa3b5"}

# Keyword -> category. Checked against the lower-cased recipient name.
RULES = {
    "food": ["swiggy", "zomato", "restaurant", "cafe", "café", "domino", "pizza", "mcdonald", "kfc",
             "burger", "starbucks", "chai", "dhaba", "eatsure", "haldiram", "biryani", "bakery"],
    "travel": ["uber", "ola", "rapido", "irctc", "flight", "indigo", "air india", "makemytrip",
               "redbus", "metro", "fuel", "petrol", "indian oil", "fastag", "taxi", "cab"],
    "shopping": ["amazon", "flipkart", "myntra", "ajio", "meesho", "nykaa", "croma",
                 "reliance digital", "decathlon", "store", "mall"],
    "groceries": ["bigbasket", "big basket", "blinkit", "zepto", "dmart", "d-mart", "jiomart",
                  "kirana", "instamart", "supermarket", "grocery", "vegetable", "sabzi"],
    "entertainment": ["netflix", "hotstar", "prime video", "spotify", "bookmyshow", "pvr", "inox",
                      "sonyliv", "sony liv", "youtube premium", "cinema"],
    "health": ["apollo", "pharmacy", "medplus", "1mg", "netmeds", "pharmeasy", "hospital", "clinic",
               "dental", "diagnostic", "lab", "practo", "chemist", "doctor"],
    "bills": ["electricity", "jal board", "water", "gas", "broadband", "prepaid", "postpaid",
              "recharge", "bescom", "tata power", "insurance", "lic", "rent", "dth", "fiber"],
}
KIND_DEFAULT = {"person": "transfers", "biller": "bills", "recharge": "bills", "gold": "other"}


def categorize(recipient_name: str, kind: str = None) -> str:
    name = (recipient_name or "").lower()
    for category, words in RULES.items():
        if any(w in name for w in words):
            return category
    return KIND_DEFAULT.get(kind or "", "other")

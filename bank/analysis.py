"""
Spending analysis: turns (account, category, period) into a JSON "dashboard spec"
that the analysis page renders. Every number is computed by the database
(Sum/Count/Avg/Max, Trunc*), never by an LLM. Chart choice is rule-based and
depends on the data: a chart that wouldn't say anything is skipped.

    build_analysis(account, category="food", period="last_month")
"""
import calendar
import re
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, Max, Sum
from django.db.models.functions import ExtractIsoWeekDay, TruncDay, TruncMonth, TruncWeek
from django.utils import timezone

from .categories import CATEGORIES, COLORS, LABELS
from .models import Payment

CHART_TYPES = {"donut", "bar", "horizontal_bar", "line", "area", "stacked_bar",
               "calendar_heatmap", "comparison_bar"}
PERIOD_KEYS = ["this_month", "last_month", "last_7_days", "last_30_days", "last_90_days",
               "this_year", "last_year", "last_3_months", "last_6_months", "last_12_months"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ------------------------------------------------------------------ money

def inr(value) -> str:
    """₹1,23,456 (Indian digit grouping); paise only when they exist."""
    d = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    neg = d < 0
    d = abs(d)
    whole, paise = divmod(d, 1)
    s = str(int(whole))
    if len(s) > 3:                       # last 3 digits, then groups of 2: 12,34,567
        h, tail, parts = s[:-3], s[-3:], []
        while len(h) > 2:
            parts.insert(0, h[-2:]); h = h[:-2]
        if h:
            parts.insert(0, h)
        s = ",".join(parts) + "," + tail
    out = "₹" + s + (f".{int(paise * 100):02d}" if paise else "")
    return "-" + out if neg else out


def _f(v) -> float:
    return float(v or 0)


# ------------------------------------------------------------------ periods

def parse_period(value=None, today: date = None):
    """Accepts a key ("last_month"), a phrase ("last 3 months", "this year"),
    {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} or "YYYY-MM-DD..YYYY-MM-DD".
    Returns (start, end, label, key). Default: last 30 days."""
    today = today or timezone.localdate()
    if isinstance(value, dict) and value.get("start") and value.get("end"):
        start, end = date.fromisoformat(value["start"]), date.fromisoformat(value["end"])
        if end < start:
            start, end = end, start
        return start, end, f"{_d(start)} to {_d(end)}", "custom"
    text = str(value or "last_30_days").strip().lower().replace("-", "_")
    m = re.match(r"^(\d{4}_\d{2}_\d{2})\.\.(\d{4}_\d{2}_\d{2})$", text)
    if m:
        return parse_period({"start": m.group(1).replace("_", "-"), "end": m.group(2).replace("_", "-")}, today)
    text = text.replace(" ", "_")
    if text in ("this_month", "month"):
        start = today.replace(day=1)
        return start, today, "this month", "this_month"
    if text in ("last_month", "previous_month"):
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end, "last month", "last_month"
    if text in ("this_year", "year", "ytd"):
        return today.replace(month=1, day=1), today, "this year", "this_year"
    if text in ("last_year", "previous_year"):
        y = today.year - 1
        return date(y, 1, 1), date(y, 12, 31), "last year", "last_year"
    if text in ("last_week", "week"):
        text = "last_7_days"
    m = re.match(r"^(?:last|past)_(\d{1,3})_(day|days|week|weeks|month|months)$", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        n = max(1, min(n, 36 if unit.startswith("month") else 400))
        if unit.startswith("day"):
            return today - timedelta(days=n - 1), today, f"last {n} days", f"last_{n}_days"
        if unit.startswith("week"):
            return today - timedelta(days=7 * n - 1), today, f"last {n} weeks", f"last_{n}_weeks"
        return _months_back(today, n), today, f"last {n} months", f"last_{n}_months"
    return today - timedelta(days=29), today, "last 30 days", "last_30_days"


def _months_back(today: date, n: int) -> date:
    y, m = today.year, today.month - n
    while m <= 0:
        m += 12; y -= 1
    return date(y, m, min(today.day, calendar.monthrange(y, m)[1])) + timedelta(days=1)


def _d(x: date) -> str:
    return f"{x.day} {x.strftime('%b %Y')}"


# ------------------------------------------------------------------ the engine

def _base(account, start, end, category):
    qs = Payment.objects.filter(account=account, status=Payment.Status.SUCCESS,
                                created_at__date__gte=start, created_at__date__lte=end)
    if category and category != "all":
        qs = qs.filter(category=category)
    return qs


def _buckets(start: date, end: date, gran: str):
    """Every bucket start date between start and end, so gaps show as zero."""
    out = []
    if gran == "day":
        d = start
        while d <= end:
            out.append(d); d += timedelta(days=1)
    elif gran == "week":
        d = start - timedelta(days=start.weekday())
        while d <= end:
            out.append(d); d += timedelta(days=7)
    else:
        d = start.replace(day=1)
        while d <= end:
            out.append(d)
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def _bucket_label(d: date, gran: str) -> str:
    if gran == "month":
        return d.strftime("%b %Y") if d.month == 1 else d.strftime("%b")
    return f"{d.day} {d.strftime('%b')}"


TRUNC = {"day": TruncDay, "week": TruncWeek, "month": TruncMonth}


def _series(qs, start, end, gran):
    tz = timezone.get_current_timezone()
    rows = (qs.annotate(b=TRUNC[gran]("created_at", tzinfo=tz)).values("b")
            .annotate(t=Sum("amount")).order_by("b"))
    got = {r["b"].date() if hasattr(r["b"], "date") else r["b"]: _f(r["t"]) for r in rows}
    keys = _buckets(start, end, gran)
    return [_bucket_label(k, gran) for k in keys], [round(got.get(k, 0.0), 2) for k in keys], keys


def normalize_category(value) -> str:
    v = str(value or "all").strip().lower()
    return v if v in CATEGORIES else "all"


def build_analysis(account, category=None, period=None, today: date = None) -> dict:
    category = normalize_category(category)
    start, end, plabel, pkey = parse_period(period, today)
    days = (end - start).days + 1
    qs = _base(account, start, end, category)
    agg = qs.aggregate(total=Sum("amount"), count=Count("id"), avg=Avg("amount"), biggest=Max("amount"))
    total, count = _f(agg["total"]), agg["count"] or 0

    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    prev_qs = _base(account, prev_start, prev_end, category)
    prev_total = _f(prev_qs.aggregate(t=Sum("amount"))["t"])

    cat_label = "All spending" if category == "all" else f"{LABELS[category]} spending"
    spec = {
        "title": f"{cat_label}, {plabel}",
        "subtitle": f"{_d(start)} to {_d(end)}",
        "period": {"start": start.isoformat(), "end": end.isoformat(), "label": plabel, "key": pkey},
        "category": category,
        "category_label": "All categories" if category == "all" else LABELS[category],
        "color": COLORS.get(category, "#2f56e8"),
        "empty": count == 0,
        "kpis": [], "charts": [], "insights": [],
    }
    if count == 0:
        spec["message"] = (f"No {LABELS.get(category, '').lower() + ' ' if category != 'all' else ''}"
                           f"payments found for {plabel}.")
        return spec

    biggest = qs.order_by("-amount", "-created_at").first()
    if prev_total > 0:
        change = (total - prev_total) / prev_total * 100
        change_txt = f"{'+' if change >= 0 else '−'}{abs(change):.0f}%"
        change_sub = f"vs {inr(prev_total)} in the previous {days} days"
    else:
        change, change_txt, change_sub = None, "New", "Nothing spent in the previous period"
    spec["kpis"] = [
        {"id": "total", "label": "Total spent", "value": inr(total), "raw": round(total, 2)},
        {"id": "count", "label": "Payments", "value": str(count), "raw": count},
        {"id": "average", "label": "Average payment", "value": inr(round(_f(agg["avg"]), 0)), "raw": round(_f(agg["avg"]), 2)},
        {"id": "biggest", "label": "Biggest payment", "value": inr(biggest.amount),
         "sub": f"{biggest.recipient_name}, {_d(timezone.localtime(biggest.created_at).date())}", "raw": _f(biggest.amount)},
        {"id": "change", "label": "Change", "value": change_txt, "sub": change_sub,
         "raw": None if change is None else round(change, 1), "good": change is not None and change <= 0},
    ]

    charts = []
    gran = "day" if days <= 31 else ("week" if days <= 120 else "month")
    labels, values, _keys = _series(qs, start, end, gran)

    by_cat = list(qs.values("category").annotate(t=Sum("amount"), n=Count("id")).order_by("-t"))
    by_rec = list(qs.values("recipient_name").annotate(t=Sum("amount"), n=Count("id")).order_by("-t")[:7])

    if category == "all":
        if len(by_cat) >= 2:
            charts.append({"id": "share", "type": "donut", "title": "Where your money went",
                           "subtitle": "Share of total by category",
                           "data": {"labels": [LABELS[r["category"]] for r in by_cat],
                                    "values": [_f(r["t"]) for r in by_cat],
                                    "colors": [COLORS[r["category"]] for r in by_cat]},
                           "why": f"You spent across {len(by_cat)} categories, so a share view shows the split at a glance."})
        if len(labels) >= 2 and gran != "day" and len(by_cat) >= 2:
            tz = timezone.get_current_timezone()
            rows = (qs.annotate(b=TRUNC[gran]("created_at", tzinfo=tz)).values("b", "category")
                    .annotate(t=Sum("amount")))
            grid = {}
            for r in rows:
                k = r["b"].date() if hasattr(r["b"], "date") else r["b"]
                grid[(k, r["category"])] = _f(r["t"])
            keys = _buckets(start, end, gran)
            charts.append({"id": "over_time", "type": "stacked_bar",
                           "title": f"Spending by {gran}", "subtitle": "Each bar split by category",
                           "data": {"labels": [_bucket_label(k, gran) for k in keys],
                                    "series": [{"label": LABELS[c["category"]], "color": COLORS[c["category"]],
                                                "values": [round(grid.get((k, c["category"]), 0), 2) for k in keys]}
                                               for c in by_cat]},
                           "why": f"A {days}-day period is easiest to read {gran} by {gran}, and stacking shows which category drove each one."})
        elif sum(1 for v in values if v) >= 3:
            charts.append({"id": "trend", "type": "bar", "title": f"Spending by {gran}", "subtitle": "Total per " + gran,
                           "data": {"labels": labels, "values": values},
                           "why": f"Shows how your spending moved across the {days} days."})
    else:
        if len(labels) >= 3 and sum(1 for v in values if v) >= 2:
            charts.append({"id": "trend", "type": "line" if gran == "day" else "area",
                           "title": f"{LABELS[category]} spending over time", "subtitle": f"Total per {gran}",
                           "data": {"labels": labels, "values": values, "color": COLORS[category]},
                           "why": ("Daily points suit a short period." if gran == "day"
                                   else f"Grouped by {gran} so a long period stays readable.")})

    if len(by_rec) >= 2:
        charts.append({"id": "recipients", "type": "horizontal_bar", "title": "Who you paid most",
                       "subtitle": f"Top {len(by_rec)} by amount",
                       "data": {"labels": [r["recipient_name"] for r in by_rec], "values": [_f(r["t"]) for r in by_rec],
                                "color": spec["color"]},
                       "why": f"{len(by_rec)} different payees, ranked so the biggest stand out."})

    if count >= 7:
        wd = {r["d"]: _f(r["t"]) for r in qs.annotate(d=ExtractIsoWeekDay("created_at")).values("d").annotate(t=Sum("amount"))}
        charts.append({"id": "weekday", "type": "bar", "title": "Which days you spend",
                       "subtitle": "Total by day of the week",
                       "data": {"labels": WEEKDAYS, "values": [round(wd.get(i, 0), 2) for i in range(1, 8)],
                                "color": spec["color"]},
                       "why": f"With {count} payments there is enough data to see a weekly habit."})

    if days >= 21 and count >= 10 and days <= 190:
        dl, dv, dk = _series(qs, start, end, "day")
        charts.append({"id": "calendar", "type": "calendar_heatmap", "title": "Day by day",
                       "subtitle": "Darker means more spent",
                       "data": {"days": [{"date": k.isoformat(), "value": v} for k, v in zip(dk, dv)],
                                "color": spec["color"]},
                       "why": "Over three weeks or more, a calendar shows busy and quiet days."})

    if prev_total > 0:
        if category == "all":
            prev_cat = {r["category"]: _f(r["t"]) for r in prev_qs.values("category").annotate(t=Sum("amount"))}
            top = [r["category"] for r in by_cat[:5]]
            data = {"labels": [LABELS[c] for c in top],
                    "series": [{"label": "This period", "values": [_f(next(r["t"] for r in by_cat if r["category"] == c)) for c in top], "color": "#2f56e8"},
                               {"label": "Previous period", "values": [prev_cat.get(c, 0) for c in top], "color": "#b9c3d6"}]}
        else:
            data = {"labels": [LABELS[category]],
                    "series": [{"label": "This period", "values": [total], "color": COLORS[category]},
                               {"label": "Previous period", "values": [prev_total], "color": "#b9c3d6"}]}
        charts.append({"id": "compare", "type": "comparison_bar", "title": "Compared with before",
                       "subtitle": f"{_d(start)} to {_d(end)} vs the {days} days before",
                       "data": data, "why": "You also spent in the previous period, so a side-by-side shows the change."})

    spec["charts"] = [c for c in charts if c["type"] in CHART_TYPES]

    # --- insights (numbers only from the queries above) ---
    ins = []
    day_rows = (qs.annotate(d=TruncDay("created_at", tzinfo=timezone.get_current_timezone()))
                .values("d").annotate(t=Sum("amount")).order_by("-t"))
    if day_rows:
        top_day = day_rows[0]
        ins.append(f"Your biggest day was {_d(top_day['d'].date() if hasattr(top_day['d'], 'date') else top_day['d'])}, with {inr(top_day['t'])} spent.")
    if by_rec:
        share = _f(by_rec[0]["t"]) / total * 100
        ins.append(f"{by_rec[0]['recipient_name']} took {share:.0f}% of this spending ({inr(by_rec[0]['t'])}).")
    if category == "all" and by_cat:
        ins.append(f"{LABELS[by_cat[0]['category']]} was your biggest category at {inr(by_cat[0]['t'])}.")
    if change is not None:
        ins.append(f"You spent {abs(change):.0f}% {'more' if change > 0 else 'less'} than in the previous {days} days.")
    if count >= 7:
        weekend = _f(qs.filter(created_at__week_day__in=[1, 7]).aggregate(t=Sum("amount"))["t"])
        ins.append(f"{weekend / total * 100:.0f}% of it was spent on weekends.")
    spec["insights"] = ins[:5]
    return spec


# ------------------------------------------------------------------ voice phrases

CATEGORY_WORDS = {
    "food": ["food", "eating", "restaurant", "swiggy", "zomato", "khana", "खाना", "भोजन"],
    "travel": ["travel", "transport", "cab", "uber", "ola", "trip", "yatra", "यात्रा", "सफर"],
    "shopping": ["shopping", "shop", "amazon", "flipkart", "खरीदारी"],
    "bills": ["bill", "bills", "utility", "utilities", "electricity", "recharge", "बिल", "बिजली"],
    "entertainment": ["entertainment", "movies", "movie", "netflix", "मनोरंजन"],
    "health": ["health", "medical", "medicine", "doctor", "pharmacy", "दवा", "दवाई", "स्वास्थ्य"],
    "groceries": ["grocery", "groceries", "sabzi", "vegetables", "किराना", "राशन"],
    "transfers": ["transfer", "transfers", "family", "friends", "sent to people"],
}


def category_from_text(text: str) -> str:
    t = text.lower()
    for cat, words in CATEGORY_WORDS.items():
        if any(w in t for w in words):
            return cat
    return "all"


def period_from_text(text: str) -> str:
    t = text.lower()
    m = re.search(r"(?:last|past|pichle|पिछले)\s+(\d{1,2})\s+(day|days|week|weeks|month|months|din|दिन|mahine|महीने)", t)
    if m:
        unit = m.group(2)
        unit = "days" if unit in ("day", "days", "din", "दिन") else "weeks" if unit.startswith("week") else "months"
        return f"last_{m.group(1)}_{unit}"
    if any(w in t for w in ["last month", "previous month", "pichle mahine", "पिछले महीने", "पिछला महीना"]):
        return "last_month"
    if any(w in t for w in ["this month", "is mahine", "इस महीने"]):
        return "this_month"
    if any(w in t for w in ["last year", "pichle saal", "पिछले साल"]):
        return "last_year"
    if any(w in t for w in ["this year", "is saal", "इस साल", "overall", "so far"]):
        return "this_year"
    if any(w in t for w in ["last week", "this week", "pichle hafte", "पिछले हफ्ते"]):
        return "last_7_days"
    return "last_30_days"


def save_request(account, category, period) -> dict:
    """Save an analysis request and return {"id", "url"}. If saving fails (for
    example a database problem), fall back to a plain link with the same
    parameters, so the assistant still answers instead of crashing."""
    from urllib.parse import urlencode

    from django.db import DatabaseError, transaction

    from .models import Analysis
    params = {"category": normalize_category(category), "period": period or "last_30_days"}
    try:
        with transaction.atomic():
            a = Analysis.objects.create(account=account, params=params)
        return {"id": str(a.id), "url": f"/analysis/{a.id}/"}
    except DatabaseError:
        import logging
        logging.getLogger(__name__).exception("Could not save analysis; using a plain link")
        return {"id": None, "url": "/analysis/?" + urlencode(params)}

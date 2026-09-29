#!/usr/bin/env python3
"""Competitor booking tracker.

Bandit Golf (AllWedge): booked slots disappear from the calendar, and past
slots for today are hidden. We record slots that are missing from the normal
schedule (5:00 AM - 3:00 AM; 3-5 AM is always closed).

TARGET Ottawa (Skedda): bookings are shown as blocks on a day view, including
past ones. We record each block.

Results are merged into data/bookings.csv (overlapping sightings of the same
booking are combined) and rolled up into data/weekly_summary.csv.
"""
import argparse
import csv
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Toronto")
DATA = Path(__file__).parent / "data"
BOOKINGS = DATA / "bookings.csv"
WEEKLY = DATA / "weekly_summary.csv"
FIELDS = ["venue", "date", "bay", "start", "end", "hours", "prime_hours", "first_seen", "last_seen"]

# Local (Ottawa) hours at which each check runs
SCHEDULE = {"bandit": {9, 12, 17, 18, 19, 20, 21, 22, 23}, "target": {23}}

VENUES = {
    "bandit": {"name": "Bandit Golf", "bays": 3, "hours_per_day": 22,
               "url": "https://book.allwedge.com/venue/bandit-golf/book"},
    "target": {"name": "TARGET Ottawa", "bays": 8, "hours_per_day": 24,
               "url": "https://targetgolfottawa.skedda.com/booking?viewdate={d}"},
}

# Bandit's normal half-hour slot starts for a given date (minutes after midnight)
BANDIT_SLOTS = list(range(0, 180, 30)) + list(range(300, 1440, 30))
DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------- helpers
def to_min(t: str) -> int:
    """'8:30 AM' / '8:30 a.m.' -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([ap])\.?m\.?\s*$", t.strip(), re.I)
    if not m:
        raise ValueError(f"Unrecognized time: {t!r}")
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    h = h % 12 + (12 if ap == "p" else 0)
    return h * 60 + mi


def hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def from_hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def prime_hours(d: date, s: int, e: int) -> float:
    """Overlap with prime time: weeknights 5-11pm, weekends 8am-10pm."""
    lo, hi = (17 * 60, 23 * 60) if d.weekday() < 5 else (8 * 60, 22 * 60)
    return max(0, min(e, hi) - max(s, lo)) / 60


def group(mins):
    """Group sorted half-hour slot starts into [start, end) intervals."""
    out = []
    for m in sorted(mins):
        if out and m == out[-1][1]:
            out[-1][1] = m + 30
        else:
            out.append([m, m + 30])
    return [tuple(x) for x in out]


def bandit_column(times, d, today, now_min, is_last):
    """Given one bay's shown slot labels (top to bottom) on the tab for date d,
    return booked intervals for date d. Slots after the overnight closure that
    belong to d+1 are ignored (they're counted on d+1's tab)."""
    shown, daytime = set(), False
    for t in times:
        m = to_min(t)
        if m >= 300:
            daytime = True
        if daytime and m < 180:
            continue  # early-morning slot of the next day
        shown.add(m)
    if not shown:
        return None  # nothing shown: closed or fully booked; don't guess
    expected = [m for m in BANDIT_SLOTS if not (d == today and m <= now_min)]
    if is_last:
        expected = [m for m in expected if m <= max(shown)]
    return group(set(expected) - shown)


def load_rows():
    if not BOOKINGS.exists():
        return []
    with BOOKINGS.open(newline="") as f:
        return list(csv.DictReader(f))


def add_booking(rows, venue, d, bay, s, e, seen):
    ds = d.isoformat()
    overlapping = [r for r in rows if r["venue"] == venue and r["date"] == ds and r["bay"] == bay
                   and s < from_hhmm(r["end"]) and from_hhmm(r["start"]) < e]
    first = seen
    for r in overlapping:
        s, e = min(s, from_hhmm(r["start"])), max(e, from_hhmm(r["end"]))
        first = min(first, r["first_seen"])
        rows.remove(r)
    rows.append({"venue": venue, "date": ds, "bay": bay, "start": hhmm(s), "end": hhmm(e),
                 "hours": f"{(e - s) / 60:.2f}", "prime_hours": f"{prime_hours(d, s, e):.2f}",
                 "first_seen": first, "last_seen": seen})
    return 0 if overlapping else 1


def save(rows):
    DATA.mkdir(exist_ok=True)
    rows.sort(key=lambda r: (r["venue"], r["date"], r["bay"], r["start"]))
    with BOOKINGS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    weeks = {}
    for r in rows:
        d = date.fromisoformat(r["date"])
        k = (r["venue"], (d - timedelta(days=d.weekday())).isoformat())
        h, p = weeks.get(k, (0.0, 0.0))
        weeks[k] = (h + float(r["hours"]), p + float(r["prime_hours"]))
    cap = {v["name"]: v["bays"] * v["hours_per_day"] * 7 for v in VENUES.values()}
    with WEEKLY.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["venue", "week_starting_monday", "booked_hours", "prime_hours",
                    "capacity_hours", "utilization"])
        for (v, wk), (h, p) in sorted(weeks.items()):
            w.writerow([v, wk, f"{h:.1f}", f"{p:.1f}", cap.get(v, ""),
                        f"{h / cap[v]:.1%}" if v in cap else ""])


# ---------------------------------------------------------------- scrapers
JS_BANDIT = r"""
() => {
  const isTime = t => /^\d{1,2}:\d{2} (AM|PM)$/.test(t);
  const slots = [...document.querySelectorAll('button')]
    .filter(b => isTime((b.innerText || '').trim()))
    .map(b => { const r = b.getBoundingClientRect(); return {t: b.innerText.trim(), x: r.left, y: r.top}; });
  const cands = [...document.querySelectorAll('div,button,a,li')].filter(e => {
    const lines = (e.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    return lines.length >= 2 && lines.length <= 4 && /\/hr$/.test(lines[lines.length - 1]);
  });
  const cards = cands.filter(e => !cands.some(o => o !== e && e.contains(o)))
    .map(e => ({name: e.innerText.split('\n')[0].trim(), x: e.getBoundingClientRect().left}));
  return {slots, cards};
}
"""

JS_TABS = r"""
() => [...document.querySelectorAll('button')]
  .map(b => (b.innerText || '').replace(/\s+/g, ' ').trim())
  .filter(t => /^(Today|(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2})$/.test(t))
"""

JS_CLICK_TAB = r"""
(label) => {
  const b = [...document.querySelectorAll('button')]
    .find(b => (b.innerText || '').replace(/\s+/g, ' ').trim() === label);
  if (b) b.click();
  return !!b;
}
"""

JS_TARGET = r"""
() => {
  const heads = [...document.querySelectorAll('*')]
    .filter(e => /^Ottawa #\d/.test((e.textContent || '').trim()) && e.textContent.trim().length < 40);
  const hs = heads.filter(e => !heads.some(o => o !== e && e.contains(o)))
    .map(e => { const r = e.getBoundingClientRect(); return {name: e.textContent.trim().split(' /')[0], l: r.left, r: r.right}; });
  const re = /\d{1,2}:\d{2}\s*[ap]\.m\.\s*[–-]\s*\d{1,2}:\d{2}\s*[ap]\.m\./;
  const bl = [...document.querySelectorAll('*')]
    .filter(e => re.test(e.textContent || '') && (e.textContent || '').length < 60);
  const leaf = bl.filter(e => !bl.some(o => o !== e && e.contains(o)));
  return {heads: hs.length, blocks: leaf.map(e => {
    const x = e.getBoundingClientRect().left + 3;
    const h = hs.find(h => x >= h.l - 4 && x <= h.r);
    return {t: e.textContent.trim().match(re)[0], bay: h ? h.name : null};
  })};
}
"""


def tab_date(label, today):
    if label == "Today":
        return today
    abbr, num = label.split()
    for i in range(0, 14):
        d = today + timedelta(days=i)
        if DAY_ABBR[d.weekday()] == abbr and d.day == int(num):
            return d
    raise ValueError(f"Can't map tab {label!r} to a date")


def check_bandit(page, rows, now):
    v = VENUES["bandit"]
    page.goto(v["url"], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_function("() => [...document.querySelectorAll('button')].some(b => (b.innerText || '').trim() === 'Today')", timeout=45000)
    page.wait_for_timeout(2000)
    tabs = page.evaluate(JS_TABS)
    if not tabs:
        raise RuntimeError("Bandit: no day tabs found - page layout may have changed")
    today, now_min, seen, added = now.date(), now.hour * 60 + now.minute, now.strftime("%Y-%m-%d %H:%M"), 0
    for i, label in enumerate(tabs):
        page.evaluate(JS_CLICK_TAB, label)
        page.wait_for_timeout(2500)
        d = tab_date(label, today)
        data = page.evaluate(JS_BANDIT)
        cards = sorted(data["cards"], key=lambda c: c["x"])
        if not cards:
            raise RuntimeError("Bandit: bay headers not found - page layout may have changed")
        cols = {c["name"]: [] for c in cards}
        for s in sorted(data["slots"], key=lambda s: s["y"]):
            bay = min(cards, key=lambda c: abs(c["x"] - s["x"]))["name"]
            cols[bay].append(s["t"])
        for bay, times in cols.items():
            ivs = bandit_column(times, d, today, now_min, is_last=(i == len(tabs) - 1))
            if ivs is None:
                print(f"  warn: {d} {bay}: no slots shown (closed or fully booked) - skipped")
                continue
            for s, e in ivs:
                added += add_booking(rows, v["name"], d, bay, s, e, seen)
    print(f"Bandit: {len(tabs)} days checked, {added} new bookings")


def check_target(page, rows, now):
    v = VENUES["target"]
    seen, added = now.strftime("%Y-%m-%d %H:%M"), 0
    for d in (now.date() - timedelta(days=1), now.date()):  # yesterday catches late bookings
        page.goto(v["url"].format(d=d.isoformat()), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("text=Ottawa #1", timeout=45000)
        page.wait_for_timeout(3000)
        data = page.evaluate(JS_TARGET)
        if data["heads"] == 0:
            raise RuntimeError("TARGET: bay headers not found - page layout may have changed")
        for b in data["blocks"]:
            if not b["bay"]:
                print(f"  warn: {d} block {b['t']} not matched to a bay - skipped")
                continue
            a, z = re.split(r"\s*[–-]\s*", b["t"])
            s, e = to_min(a), to_min(z)
            if e <= s:
                e = 1440  # runs past midnight; count only today's portion
            added += add_booking(rows, v["name"], d, b["bay"], s, e, seen)
    print(f"TARGET: {added} new bookings")


# ---------------------------------------------------------------- main
def gate(event, forced):
    if event == "workflow_dispatch":
        tasks = [t for t in (forced or "bandit,target").split(",") if t in VENUES]
    else:
        h = datetime.now(TZ).hour
        tasks = [k for k, hours in SCHEDULE.items() if h in hours]
    print(f"tasks={','.join(tasks)}")


def debug(page, name):
    """Save what the page looked like when a check failed."""
    try:
        DATA.mkdir(exist_ok=True)
        page.screenshot(path=str(DATA / f"debug_{name}.png"), full_page=True)
        print(f"--- {name} page at failure: {page.url} ---")
        print(page.evaluate("() => document.body ? document.body.innerText.slice(0, 1500) : '(no body)'"))
    except Exception as ex:
        print(f"(debug capture failed: {ex})")


def run(tasks):
    from playwright.sync_api import sync_playwright
    rows, now = load_rows(), datetime.now(TZ)
    print(f"Run at {now:%Y-%m-%d %H:%M %Z}: {tasks}")
    failed = False
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000},
                                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                           "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
        for t in tasks:
            try:
                {"bandit": check_bandit, "target": check_target}[t](page, rows, now)
            except Exception as ex:  # keep other venue's results if one fails
                failed = True
                print(f"ERROR in {t}: {ex}", file=sys.stderr)
                debug(page, t)
        browser.close()
    save(rows)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", nargs="*")
    ap.add_argument("--run")
    a = ap.parse_args()
    if a.gate is not None:
        gate(*(a.gate + ["", ""])[:2])
    elif a.run:
        run([t for t in a.run.split(",") if t in VENUES])
    else:
        ap.print_help()

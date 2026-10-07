"""
Dolores Park tennis bot (rec.us / SF Rec & Park).

Books a 6:00 PM weekday tennis court at Dolores Park.

Two booking windows (from the Dolores page on rec.us):
  * morning: Courts 1 & 2, open 7 days ahead at 8:00 AM PT (90-min slots, 6:00-7:30 PM)
  * noon:    Court 3,      opens 2 days ahead at 12:00 PM PT (60-min slots, 6:00-7:00 PM)

Rules enforced: Mon-Fri targets only, max 1 booking per day, max 3 per calendar week (Sun-Sat).

Env vars:
  REC_EMAIL, REC_PASSWORD  (required)
  MODE          auto | morning | noon      (default auto: picked from the current PT time)
  TARGET_DATE   YYYY-MM-DD override        (default: today+7 for morning, today+2 for noon)
  SLOT_TIME     e.g. "6:00 PM"             (default 6:00 PM; change only for testing)
  DRY_RUN       true | false               (true = do everything except click "Book")
  NO_WAIT       true | false               (true = don't wait for the opening time; for tests)
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PT = ZoneInfo("America/Los_Angeles")
LOCATION_ID = "95745483-6b38-4e99-8ba2-a3e23cda8587"  # Dolores
LOCATION_URL = f"https://www.rec.us/locations/{LOCATION_ID}"
SCHEDULE_API = f"https://api.rec.us/v1/locations/{LOCATION_ID}/schedule?startDate={{d}}"
BOOKINGS_URL = "https://www.rec.us/app/schedule"

WINDOWS = {
    "morning": {"open_hour": 8, "days_ahead": 7, "courts": ["Court 1", "Court 2"]},  # 8 AM
    "noon": {"open_hour": 12, "days_ahead": 2, "courts": ["Court 3"]},
}
WEEKLY_CAP = 3
SHOTS = Path("screenshots")
SHOTS.mkdir(exist_ok=True)


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes")


def log(msg):
    print(f"[{datetime.now(PT):%H:%M:%S.%f}"[:-3] + f"] {msg}", flush=True)


def write_result(status, message):
    """Result file read by the GitHub workflow to decide whether to notify you."""
    Path("result.json").write_text(json.dumps({"status": status, "message": message}))
    log(f"RESULT {status}: {message}")


def slot_minutes(label):  # "6:00 PM" -> 1080
    t = datetime.strptime(label.strip().upper(), "%I:%M %p")
    return t.hour * 60 + t.minute


# ---------- public schedule API (no login needed) ----------

def slot_is_open(target: date, courts, slot_min) -> bool:
    url = SCHEDULE_API.format(d=target.isoformat())
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.load(r)
    for court in data.get("dates", {}).get(target.strftime("%Y%m%d"), []):
        if court.get("courtNumber") not in courts:
            continue
        for rng, info in court.get("schedule", {}).items():
            start, end = [x.strip() for x in rng.split(",")]
            s = int(start[:2]) * 60 + int(start[3:])
            e = int(end[:2]) * 60 + int(end[3:])
            if s <= slot_min < e and info.get("referenceType") == "RESERVABLE":
                return True
    return False


# ---------- browser steps ----------

def dismiss_cookies(page):
    try:
        page.get_by_role("button", name="Deny Non-Essential").click(timeout=2000)
    except PWTimeout:
        pass


def login(page, email, password):
    page.goto(LOCATION_URL, wait_until="domcontentloaded")
    dismiss_cookies(page)
    page.get_by_role("button", name="Log in").first.click()
    dlg = page.locator("[role=dialog]").filter(has=page.locator("input[type=password]"))
    dlg.locator("input:not([type=password])").first.fill(email)
    dlg.locator("input[type=password]").fill(password)
    dlg.locator("button[type=submit]").click()
    page.get_by_role("button", name="View Bookings").wait_for(timeout=20000)
    log("Logged in")


def my_booking_dates(page):
    """Dates (PT) of your upcoming bookings, read from the Bookings page's data."""
    found = []

    def walk(obj, key=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, k)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, key)
        elif isinstance(obj, str) and re.search(r"start|from|begin", key, re.I):
            m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", obj)
            if m:
                try:
                    dt = datetime.fromisoformat(obj.replace("Z", "+00:00"))
                    if dt.tzinfo:
                        dt = dt.astimezone(PT)
                    found.append(dt.date())
                except ValueError:
                    found.append(date.fromisoformat(m.group(1)))

    try:
        with page.expect_response(lambda r: "planned-bookings" in r.url, timeout=15000) as resp:
            page.goto(BOOKINGS_URL, wait_until="domcontentloaded")
        walk(resp.value.json())
    except Exception as e:  # never block a booking because this check failed
        log(f"Could not read existing bookings ({e}); continuing")
    return sorted(set(found))


def select_date(page, target: date):
    page.get_by_role("button", name="Select date").click()
    cell = page.locator(f'td[data-day="{target.isoformat()}"] button')
    if cell.count() == 0:  # target is in next month's view
        page.get_by_role("button", name=re.compile("next month", re.I)).click()
    cell.click(timeout=5000)


def try_book(page, target, slot_label, courts, dry_run):
    page.goto(LOCATION_URL, wait_until="domcontentloaded")
    select_date(page, target)
    slot = page.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(slot_label)}"))
    slot.first.click(timeout=8000)

    dlg = page.locator("[role=dialog]").filter(has_text="Site reservation")
    dlg.wait_for(timeout=8000)

    # Court: make sure it's one of the courts for this window
    site_box = dlg.get_by_role("combobox").nth(2)
    current = site_box.inner_text().strip()
    if current not in courts:
        site_box.click()
        opts = page.get_by_role("option")
        picked = False
        for c in courts:
            o = opts.filter(has_text=re.compile(rf"^\s*{c}\s*$"))
            if o.count():
                o.first.click()
                picked = True
                break
        if not picked:
            raise RuntimeError(f"None of {courts} offered at {slot_label} (saw {current})")

    # Participant: you (Account Owner)
    dlg.get_by_role("combobox").filter(has_text="Select participant").click()
    page.get_by_role("option").filter(has_text="Account Owner").first.click()

    court = site_box.inner_text().strip()
    summary = dlg.inner_text()[:300].replace("\n", " | ")
    page.screenshot(path=str(SHOTS / "before_book.png"), full_page=True)
    log(f"Ready to book: {court} | {summary}")

    if dry_run:
        return f"DRY RUN: would book {court} on {target:%a %b %d} at {slot_label}"

    dlg.get_by_role("button", name="Book", exact=True).click()
    page.wait_for_timeout(4000)
    # If a payment/confirm step appears, finish it (uses the card saved in your rec.us account).
    nxt = page.get_by_role("button", name=re.compile(r"^(Pay|Confirm|Complete|Place order|Checkout)", re.I))
    if nxt.count() and nxt.first.is_visible():
        log("Confirm/payment step shown; completing it")
        nxt.first.click()
        page.wait_for_timeout(5000)
    page.screenshot(path=str(SHOTS / "after_book.png"), full_page=True)
    return court


def main():
    email, password = os.environ["REC_EMAIL"], os.environ["REC_PASSWORD"]
    dry_run = env_bool("DRY_RUN", True)
    no_wait = env_bool("NO_WAIT")
    slot_label = os.environ.get("SLOT_TIME") or "6:00 PM"
    slot_min = slot_minutes(slot_label)

    now = datetime.now(PT)
    mode = (os.environ.get("MODE") or "auto").lower()
    if mode == "auto":
        mode = "morning" if now.hour < 10 else "noon"
    w = WINDOWS[mode]
    open_at = now.replace(hour=w["open_hour"], minute=0, second=0, microsecond=0)

    # GitHub runs each schedule twice (one per daylight-saving offset); skip the wrong one.
    if not no_wait and not (open_at - timedelta(minutes=40) <= now <= open_at + timedelta(minutes=40)):
        write_result("skip", f"Not near the {mode} opening time (now {now:%H:%M} PT)")
        return

    target = (date.fromisoformat(os.environ["TARGET_DATE"]) if os.environ.get("TARGET_DATE")
              else now.date() + timedelta(days=w["days_ahead"]))
    if target.weekday() > 4:
        write_result("skip", f"{target:%a %b %d} is a weekend")
        return
    log(f"Mode={mode} target={target:%a %Y-%m-%d} slot={slot_label} courts={w['courts']} dry_run={dry_run}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 1000})
        try:
            login(page, email, password)

            booked = my_booking_dates(page)
            week_start = target - timedelta(days=(target.weekday() + 1) % 7)  # Sunday
            in_week = [d for d in booked if week_start <= d < week_start + timedelta(days=7)]
            log(f"Existing bookings: {booked} (this week: {len(in_week)})")
            if target in booked:
                write_result("skip", f"Already booked on {target:%a %b %d}")
                return
            if len(in_week) >= WEEKLY_CAP:
                write_result("skip", f"Weekly cap reached ({len(in_week)} bookings week of {week_start:%b %d})")
                return

            # Wait for the window to open, then poll until the slot shows as reservable.
            if not no_wait:
                page.goto(LOCATION_URL, wait_until="domcontentloaded")
                while datetime.now(PT) < open_at - timedelta(seconds=2):
                    time.sleep(min(20, max(0.2, (open_at - datetime.now(PT)).total_seconds() - 2)))
            deadline = time.time() + 180
            while True:
                try:
                    if slot_is_open(target, w["courts"], slot_min):
                        break
                except Exception as e:
                    log(f"schedule check error: {e}")
                if time.time() > deadline:
                    msg = f"No {slot_label} slot open on {', '.join(w['courts'])} for {target:%a %b %d}"
                    # A morning miss is fine (noon run on Court 3 is the backup); a noon miss is final.
                    write_result("miss" if mode == "morning" else "fail", msg)
                    return
                time.sleep(0.7)
            log("Slot is open; booking")

            for attempt in range(1, 4):
                try:
                    court = try_book(page, target, slot_label, w["courts"], dry_run)
                    break
                except Exception as e:
                    log(f"Attempt {attempt} failed: {e}")
                    page.screenshot(path=str(SHOTS / f"error_{attempt}.png"), full_page=True)
                    if attempt == 3:
                        raise
            if dry_run:
                write_result("dry_run", court)
                return

            # Verify it really shows up in your bookings
            if target in my_booking_dates(page):
                write_result("booked", f"Booked {court} at Dolores, {target:%a %b %d} {slot_label}")
            else:
                write_result("fail", f"Clicked Book for {court} on {target:%a %b %d} but it isn't in your "
                                     f"bookings. Check the screenshots / rec.us.")
        except Exception as e:
            page.screenshot(path=str(SHOTS / "crash.png"), full_page=True)
            write_result("fail", f"Bot error: {e}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
    res = json.loads(Path("result.json").read_text())
    sys.exit(1 if res["status"] == "fail" else 0)

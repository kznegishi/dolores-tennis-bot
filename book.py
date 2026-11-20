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
LOCATION_ID = "8c3b9b04-a149-4080-b648-e3ff8365bbee"  # Hamilton
LOCATION_URL = f"https://www.rec.us/locations/{LOCATION_ID}"
PARKS = {"Hamilton": {"id": "8c3b9b04-a149-4080-b648-e3ff8365bbee", "morning": ["Court 1"], "noon": ["Court 2"], "noon_min": 90, "acts": {"Court 1": "e9b0665a-debc-4766-8cff-a28f8ed31a8e", "Court 2": "7545080e-502e-4e8d-a3e5-24432933e06b"}}, "Dolores": {"id": "95745483-6b38-4e99-8ba2-a3e23cda8587", "morning": ["Court 1", "Court 2"], "noon": ["Court 3"], "noon_min": 60, "acts": {"Court 1": "1bcdebfc-cb10-45e8-a914-b9e7a99bf9cb", "Court 2": "06af27d8-eded-4d99-9c3f-5b2e17ff9730", "Court 3": "e7220541-0d10-45a1-95a0-0cade5b20720"}}}; DAY_PARK = {1: "Hamilton", 2: "Dolores", 3: "Dolores"}; LOCATION_ID = PARKS["Dolores"]["id"]  # Tue = Hamilton, Wed/Thu = Dolores
BOOKINGS_URL = "https://www.rec.us/app/schedule"

WINDOWS = {
    "morning": {"open_hour": 8, "days_ahead": 7, "courts": ["Court 1"], "minutes": 90},  # 8 AM
    "noon": {"open_hour": 12, "days_ahead": 2, "courts": ["Court 2"], "minutes": 90},
}
# rec.us "site activity" IDs (tennis) for each Dolores court, used by the direct booking request.
SITE_ACTIVITY = {
    "Court 1": "e9b0665a-debc-4766-8cff-a28f8ed31a8e",  # Hamilton
    "Court 2": "7545080e-502e-4e8d-a3e5-24432933e06b",  # Hamilton
    "Court 3": "e7220541-0d10-45a1-95a0-0cade5b20720",
}
API_RESERVE = "https://api.rec.us/v1/reservations"
# Direct request is ON. rec.us's own flow is: verify the texted code -> create the reservation
# -> checkout. So the bot verifies your code a few minutes BEFORE 8:00, then fires the
# reservation request itself at 8:00:00.000 (~0.1-0.3s), then pays during the 10-minute hold.
FAST_PATH = True
SESSION = {"auth": None, "user_id": None, "issue": None}  # filled in from the logged-in browser's own requests
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

def slot_status(page, target: date, courts, slot_min) -> dict:
    """{'Court 1': 'RESERVABLE' | 'RESERVATION' | 'missing' ...} for the slot we want."""
    # Fetched from inside the logged-in browser (rec.us blocks non-browser requests).
    url = SCHEDULE_API.format(d=target.isoformat())
    data = page.evaluate(
        "async u => { const r = await fetch(u); if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }",
        url)
    status = {c: "missing" for c in courts}
    for court in data.get("dates", {}).get(target.strftime("%Y%m%d"), []):
        if court.get("courtNumber") not in courts:
            continue
        for rng, info in court.get("schedule", {}).items():
            start, end = [x.strip() for x in rng.split(",")]
            s = int(start[:2]) * 60 + int(start[3:])
            e = int(end[:2]) * 60 + int(end[3:])
            if s <= slot_min < e:
                status[court["courtNumber"]] = info.get("referenceType", "?")
    return status


def slot_is_open(page, target: date, courts, slot_min) -> bool:
    return "RESERVABLE" in slot_status(page, target, courts, slot_min).values()


# ---------- browser steps ----------

HIDE_COOKIE_BANNER = """
(() => {
  const css = '.osano-cm-window, .osano-cm-dialog, [aria-label="Cookie Consent Banner"] { display: none !important; pointer-events: none !important; }';
  const add = () => {
    const root = document.head || document.documentElement;
    if (!root || document.getElementById('no-cookie-banner')) return;
    const s = document.createElement('style'); s.id = 'no-cookie-banner'; s.textContent = css;
    root.appendChild(s);
  };
  add(); document.addEventListener('DOMContentLoaded', add);
  setInterval(add, 250);  // re-add if the page rebuilds <head>
})();
"""


def dismiss_cookies(page):
    """Decline non-essential cookies if the banner is up (it is also hidden by CSS on every page)."""
    try:
        btn = page.get_by_role("button", name="Deny Non-Essential")
        if btn.count() and btn.first.is_visible():
            btn.first.click(timeout=2000)
    except Exception:
        pass


def login(page, email, password):
    page.goto(LOCATION_URL, wait_until="domcontentloaded")
    dismiss_cookies(page)
    page.get_by_role("button", name="Log in").first.click()
    dlg = page.locator("[role=dialog]").filter(has=page.locator("input[type=password]"))
    dlg.get_by_role("textbox").first.fill(email)  # visible email box (skips hidden inputs)
    dlg.locator("input[type=password]").fill(password)
    dlg.locator("button[type=submit]").click()
    try:
        page.locator('a[href="/app/fast-track"], button:has-text("Bookings")').first.wait_for(timeout=25000)
    except PWTimeout:
        raise RuntimeError("Login didn't complete. Page said: " + page_excerpt(page))
    log("Logged in")


def page_excerpt(page, n=500):
    """Visible text from the page (dialogs first), for error messages. Never includes the password."""
    try:
        txt = page.evaluate("""() => {
            const d = [...document.querySelectorAll('[role=dialog],[role=alert]')].map(e => e.innerText).join(' / ');
            return (d + ' || ' + document.body.innerText).replace(/\\s+/g, ' ');
        }""")
    except Exception as e:
        txt = f"(couldn't read page: {e})"
    return f"[{page.url}] " + re.sub(r"\S+@\S+", "<email>", txt)[:n]


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
    cell.wait_for(timeout=3000)
    if cell.is_disabled():
        raise RuntimeError("date not open yet")  # booking window hasn't opened on the site; reload and retry
    cell.click(timeout=3000)


def try_book(page, target, slot_label, courts, dry_run):
    page.goto(LOCATION_URL, wait_until="domcontentloaded")
    dismiss_cookies(page)
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

    SESSION["clicked_book"] = True
    dlg.get_by_role("button", name="Book", exact=True).click()
    log(f"Clicked Book for {court}")
    issue_num = handle_text_code(page, court, target)
    if not issue_num:
        raise RuntimeError(f"Book didn't hold {court} (no text-code step appeared)")
    SESSION["issue"] = issue_num
    page.wait_for_timeout(1000)
    # If a payment/confirm step appears, finish it (uses the card saved in your rec.us account).
    nxt = page.get_by_role("button", name=re.compile(r"^(Pay|Confirm|Complete|Place order|Checkout)", re.I))
    if nxt.count() and nxt.first.is_visible():
        log("Confirm/payment step shown; completing it")
        nxt.first.click()
        page.wait_for_timeout(5000)
    page.screenshot(path=str(SHOTS / "after_book.png"), full_page=True)
    return court


# ---------- text-code step: you reply to a GitHub issue with the code rec.us texts you ----------

def gh(method, path, data=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}",
        method=method, data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json", "User-Agent": "dolores-tennis-bot"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def wait_for_code_from_you(court, target, minutes=9.5):
    """Open a GitHub issue (you get an email / app notification) and wait for your reply with the code."""
    slot = SESSION.get("slot") or "6:00 PM"
    owner_login = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    issue = gh("POST", "/issues", {
        "title": f"🎾 Court held — reply with your rec.us code ({court}, {target:%a %b %d} {slot})",
        "body": (f"@{owner_login} rec.us is holding **{court} at Dolores, {target:%A %b %d}, {slot}** for about 10 minutes.\n\n"
                 "**Reply to this (email reply or GitHub app) with the code rec.us just texted you** — just the digits.")})
    num = issue["number"]
    SESSION["issue"] = num
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").lower()
    log(f"Waiting for your code on issue #{num}")
    stop = time.time() + minutes * 60
    while time.time() < stop:
        try:
            comments = gh("GET", f"/issues/{num}/comments")
        except Exception as e:  # network blip: keep waiting
            log(f"(retrying GitHub check: {e})")
            time.sleep(3)
            continue
        for c in comments:
            if owner and c["user"]["login"].lower() != owner:
                continue
            m = re.search(r"\b(\d{4,8})\b", c.get("body") or "")
            if m:
                log("Got your code")
                return num, m.group(1)
        time.sleep(3)
    return num, None


def finish_issue(num, text):
    try:
        gh("POST", f"/issues/{num}/comments", {"body": text})
        gh("PATCH", f"/issues/{num}", {"state": "closed"})
    except Exception as e:
        log(f"Couldn't update issue: {e}")


CODE_INPUT = 'input[name="totp"], input[autocomplete="one-time-code"], input[name*="code" i], input[inputmode="numeric"]'


def handle_text_code(page, court, target):
    """If rec.us asks for the texted code, get it from you and enter it. Returns issue number or None."""
    send = page.get_by_role("button", name=re.compile(r"send( code)?|text me|send text", re.I))
    box = page.locator(CODE_INPUT)
    for _ in range(16):  # up to ~8s for the code step to show up
        if box.count() and box.first.is_visible():
            break
        if send.count() and send.first.is_visible():
            log("Asking rec.us to text the code")
            send.first.click()
        page.wait_for_timeout(500)
    if not (box.count() and box.first.is_visible()):
        log("No code prompt seen after Book. Page: " + page_excerpt(page, 400))
        return None
    page.screenshot(path=str(SHOTS / "code_prompt.png"), full_page=True)
    num, code = wait_for_code_from_you(court, target)
    SESSION["issue"] = num
    if not code:
        finish_issue(num, "⏱️ No code received in time — the hold expired. Sorry!")
        raise RuntimeError("No code received from you within the 10-minute hold")
    box.first.fill(code)
    page.wait_for_timeout(300)
    btn = page.get_by_role("button", name=re.compile(r"verify|confirm|submit|continue|book|done", re.I))
    if btn.count():
        btn.last.click()
    page.wait_for_timeout(5000)
    page.screenshot(path=str(SHOTS / "after_code.png"), full_page=True)
    return num


def watch_session(page):
    """Remember the login token and user id that the rec.us site itself uses (never printed)."""
    def on_request(req):
        if "api.rec.us" in req.url:
            h = req.headers.get("authorization")
            if h:
                SESSION["auth"] = h
            m = re.search(r"/users/([0-9a-f-]{36})/", req.url)
            if m:
                SESSION["user_id"] = m.group(1)
    page.on("request", on_request)


def api_book(page, court, target: date, slot_min, minutes):
    """Send the same booking request the site sends when you press Book. Returns (ok, status, body)."""
    start = datetime.combine(target, datetime.min.time()) + timedelta(minutes=slot_min)
    end = start + timedelta(minutes=minutes)
    body = {
        "customerUserId": SESSION["user_id"],
        "participantUserId": SESSION["user_id"],
        "siteActivityIds": [SITE_ACTIVITY[court]],
        "from": {"date": target.isoformat(), "time": start.strftime("%H:%M:%S")},
        "to": {"date": target.isoformat(), "time": end.strftime("%H:%M:%S")},
        "pendingFormSubmissions": [],
    }
    res = page.evaluate(
        """async ({url, auth, body}) => {
            const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'Authorization': auth}, body: JSON.stringify(body)});
            return {status: r.status, text: (await r.text()).slice(0, 600)};
        }""",
        {"url": API_RESERVE, "auth": SESSION["auth"], "body": body})
    return 200 <= res["status"] < 300, res["status"], res["text"]


def api(page, method, path, body=None):
    """Call rec.us's API from inside the logged-in browser, exactly like the site does."""
    return page.evaluate(
        """async ({url, method, auth, body}) => {
            const r = await fetch(url, {method, headers: {'Content-Type': 'application/json', 'Authorization': auth},
                                        body: body == null ? undefined : JSON.stringify(body)});
            return {status: r.status, text: (await r.text()).slice(0, 800)};
        }""",
        {"url": "https://api.rec.us/" + path, "method": method, "auth": SESSION["auth"], "body": body})


def verify_code_after_hold(target, slot_label, court, page):
    """Court is already held (~10 min). Have rec.us text you a code, get your GitHub reply, verify it."""
    r = api(page, "POST", "v1/users/mobile-totp/send")
    log(f"asked rec.us to text your code: {r['status']} {r['text'][:200]}")
    owner_login = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    issue = gh("POST", "/issues", {
        "title": f"🎾 Court HELD — reply with your rec.us code ({court}, {target:%a %b %d} {slot_label})",
        "body": (f"@{owner_login} The bot grabbed **{court}, {target:%A %b %d}, {slot_label}**. rec.us holds it ~10 min.\n\n"
                 "**Reply here with just the 6 digits rec.us texted you.** The bot then pays with your saved card.")})
    num = issue["number"]
    SESSION["verify_issue"] = num
    stop = time.time() + 8.5 * 60
    owner = owner_login.lower()
    while time.time() < stop:
        try:
            comments = gh("GET", f"/issues/{num}/comments")
        except Exception as e:
            log(f"(retrying GitHub check: {e})")
            time.sleep(3)
            continue
        for c in comments:
            if owner and c["user"]["login"].lower() != owner:
                continue
            m = re.search(r"\b(\d{6})\b", c.get("body") or "")
            if m:
                v = api(page, "POST", "v1/users/mobile-totp/verify", {"code": m.group(1)})
                log(f"verify code: {v['status']} {v['text'][:200]}")
                ok = 200 <= v["status"] < 300
                try:
                    gh("POST", f"/issues/{num}/comments", {"body": "✅ Code verified — paying now." if ok
                       else "❌ rec.us rejected that code. Reply with the newest code."})
                except Exception:
                    pass
                if ok:
                    return True
        time.sleep(2)
    return False


def checkout_and_pay(page, dry_run):
    """Open the cart/checkout page (where the court is held ~10 min) and pay with your saved card."""
    page.goto("https://www.rec.us/cart/checkout", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    dismiss_cookies(page)
    page.screenshot(path=str(SHOTS / "checkout.png"), full_page=True)
    log("Checkout page: " + page_excerpt(page, 1500))
    btns = [b.strip() for b in page.locator("button").all_inner_texts() if b.strip()]
    log(f"Checkout buttons: {btns[:25]}")
    if dry_run:
        return False
    for step in range(4):  # click through up to 4 confirm/pay screens
        btn = page.get_by_role("button", name=re.compile(
            r"^\s*(continue to payment|continue|pay|place order|complete|confirm|checkout|submit|book|finish|purchase)", re.I))
        vis = [b for b in btn.all() if b.is_visible() and b.is_enabled()]
        if not vis:
            break
        label = vis[-1].inner_text().strip()
        log(f"Clicking '{label}'")
        vis[-1].click()
        page.wait_for_timeout(4000)
        page.screenshot(path=str(SHOTS / f"pay_{step}.png"), full_page=True)
        log("After click: " + page_excerpt(page, 600))
    return True


def fast_book(page, target, slot_min, w, open_at):
    """Fire booking requests right at opening time, alternating courts, for ~15 seconds."""
    if not (SESSION["auth"] and SESSION["user_id"]):
        log("Fast path unavailable (no session captured); using click-through")
        return None
    while datetime.now(PT) < open_at:
        time.sleep(0.005)
    stop = time.time() + 15
    tries = 0
    while time.time() < stop:
        for court in w["courts"]:
            tries += 1
            try:
                ok, status, text = api_book(page, court, target, slot_min, w["minutes"])
            except Exception as e:
                ok, status, text = False, "error", str(e)[:200]
            if ok or tries <= 4 or tries % 10 == 0:
                log(f"direct booking {court}: {status} {text[:300]}")
            if ok:
                return court
        time.sleep(0.15)
    log(f"Direct booking didn't succeed after {tries} tries")
    return None


def main():
    email = os.environ["REC_EMAIL"].strip()
    password = os.environ["REC_PASSWORD"].strip("\r\n")
    dry_run = env_bool("DRY_RUN", True)
    no_wait = env_bool("NO_WAIT")
    slot_label = os.environ.get("SLOT_TIME") or "6:00 PM"
    slot_min = slot_minutes(slot_label)
    SESSION["slot"] = slot_label

    now = datetime.now(PT)
    mode = (os.environ.get("MODE") or "auto").lower()
    if mode == "auto":
        mode = "morning" if now.hour < 10 else "noon"
    w = WINDOWS[mode]
    open_at = now.replace(hour=w["open_hour"], minute=0, second=0, microsecond=0)

    # GitHub runs each schedule twice (one per daylight-saving offset); skip the wrong one.
    if not no_wait and not (open_at - timedelta(minutes=420) <= now <= open_at + timedelta(minutes=3)):
        write_result("skip", f"Not near the {mode} opening time (now {now:%H:%M} PT)")
        return

    target = (date.fromisoformat(os.environ["TARGET_DATE"]) if os.environ.get("TARGET_DATE")
              else now.date() + timedelta(days=w["days_ahead"]))
    if target.weekday() not in DAY_PARK:
        write_result("skip", f"{target:%a %b %d}: no park set for that day")
        return
    global LOCATION_ID, LOCATION_URL, SCHEDULE_API; park = DAY_PARK[target.weekday()]; P = PARKS[park]; LOCATION_ID = P["id"]; LOCATION_URL = f"https://www.rec.us/locations/{LOCATION_ID}"; SCHEDULE_API = f"https://api.rec.us/v1/locations/{LOCATION_ID}/schedule?startDate={{d}}"; SITE_ACTIVITY.clear(); SITE_ACTIVITY.update(P["acts"]); w = dict(w, courts=P[mode], minutes=(90 if mode == "morning" else P["noon_min"])); SESSION["park"] = park; log(f"Park={park} Mode={mode} target={target:%a %Y-%m-%d} slot={slot_label} courts={w['courts']} dry_run={dry_run}"); time.sleep(0 if no_wait else max(0, (open_at - timedelta(minutes=12) - datetime.now(PT)).total_seconds()))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 1000})
        page.add_init_script(HIDE_COOKIE_BANNER)  # the cookie banner can never block a click
        watch_session(page)
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

            log(f"Session ready for direct booking: token={'yes' if SESSION['auth'] else 'no'}, "
                f"user={'yes' if SESSION['user_id'] else 'no'}")
            if FAST_PATH:
                # 1) refresh the login token ~25s before opening, then fire at the exact opening instant
                if not no_wait:
                    while datetime.now(PT) < open_at - timedelta(seconds=25):
                        time.sleep(1)
                    page.goto(LOCATION_URL, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    log(f"token refreshed: {'yes' if SESSION['auth'] else 'no'}")
                fast_court = fast_book(page, target, slot_min, w, open_at if not no_wait else datetime.now(PT))
                if fast_court:
                    log(f"HELD {fast_court} via direct request")
                    # 2) court is held ~10 min: now get the texted code from you
                    if not dry_run and not verify_code_after_hold(target, slot_label, fast_court, page):
                        write_result("fail", f"Held {fast_court} {target:%a %b %d} but no valid code arrived in time — "
                                             "open rec.us cart NOW if the hold is still there.")
                        return
                    # 3) pay during the hold
                    paid = checkout_and_pay(page, dry_run)
                    if dry_run:
                        write_result("dry_run", f"TEST: held {fast_court} {target:%a %b %d} {slot_label}; stopped at checkout (not paid — hold expires on its own)")
                        return
                    if target in my_booking_dates(page):
                        write_result("booked", f"Booked {fast_court} at {SESSION.get('park')}, {target:%a %b %d} {slot_label}")
                    else:
                        write_result("fail", f"Held {fast_court} {target:%a %b %d} but payment didn't finish — "
                                             "open rec.us cart NOW to pay within 10 min. Check screenshots.")
                    return
                write_result("miss" if mode == "morning" else "fail",
                             f"Direct booking didn't get {', '.join(w['courts'])} {target:%a %b %d} {slot_label}")
                return

            # Wait for the window to open, then poll until the slot shows as reservable.
            if not no_wait:
                page.goto(LOCATION_URL, wait_until="domcontentloaded")
                while datetime.now(PT) < open_at - timedelta(seconds=2):
                    time.sleep(min(20, max(0.2, (open_at - datetime.now(PT)).total_seconds() - 2)))
            deadline = time.time() + 180
            last = None
            while True:
                try:
                    st = slot_status(page, target, w["courts"], slot_min)
                    if st != last:
                        log(f"slot status: {st}")
                        last = st
                    if "RESERVABLE" in st.values():
                        break
                except Exception as e:
                    log(f"schedule check error: {e}")
                if time.time() > deadline:
                    late = (datetime.now(PT) - open_at).total_seconds() / 60
                    msg = (f"No {slot_label} slot open on {', '.join(w['courts'])} for {target:%a %b %d} "
                           f"(bot was checking from {late - 3:.0f} min after opening; status {last})")
                    # A morning miss is fine (noon run on Court 3 is the backup); a noon miss is final.
                    write_result("miss" if mode == "morning" else "fail", msg)
                    return
                time.sleep(0.4)
            # rec.us's schedule says "reservable" a little before the window really opens,
            # so never start clicking before the exact opening second.
            if not no_wait:
                while datetime.now(PT) < open_at + timedelta(milliseconds=150):
                    time.sleep(0.01)
            log("Slot is open; booking")

            courts = list(w["courts"])
            stop_at = time.time() + 40
            attempt = 0
            while True:
                attempt += 1
                try:
                    court = try_book(page, target, slot_label, courts, dry_run)
                    break
                except Exception as e:
                    log(f"Attempt {attempt} failed: {str(e).splitlines()[0][:200]}")
                    if attempt <= 6:
                        page.screenshot(path=str(SHOTS / f"error_{attempt}.png"), full_page=True)
                    if SESSION["issue"] or time.time() > stop_at:
                        raise  # never re-book once a code request has gone out
                    courts = courts[1:] + courts[:1]  # try the other court next
            if dry_run:
                write_result("dry_run", court)
                return

            # Verify it really shows up in your bookings
            if target in my_booking_dates(page):
                if SESSION["issue"]:
                    finish_issue(SESSION["issue"], f"✅ Booked {court}, {target:%a %b %d} {slot_label}. See you on court!")
                write_result("booked", f"Booked {court} at Dolores, {target:%a %b %d} {slot_label}")
            else:
                if SESSION["issue"]:
                    finish_issue(SESSION["issue"], "❌ Entered your code but the booking didn't show up. Check rec.us.")
                write_result("fail", f"Clicked Book for {court} on {target:%a %b %d} but it isn't in your "
                                     f"bookings. Check the screenshots / rec.us.")
        except Exception as e:
            page.screenshot(path=str(SHOTS / "crash.png"), full_page=True)
            log("Page at failure: " + page_excerpt(page, 800))
            write_result("fail", f"Bot error: {e}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
    res = json.loads(Path("result.json").read_text())
    sys.exit(1 if res["status"] == "fail" else 0)

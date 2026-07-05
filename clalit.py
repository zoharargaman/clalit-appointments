#!/usr/bin/env python3
"""
Clalit Appointment Searcher & Booker

Search, browse, and book appointments on the Clalit healthcare system (Israel).
Supports captcha solving via AzCaptcha, calendar navigation, and full booking flow.

Usage:
    # Search for available appointments
    python clalit.py --mode search --specialty עור --city חיפה --headless

    # Find and automatically book the best appointment
    python clalit.py --mode find-best --specialty עור --city חיפה --headless

    # Book a specific doctor on a specific date/time
    python clalit.py --mode book --guid <guid> --date 28.08.2026 --time 13:40 --headless

    # List available specialties
    python clalit.py --list-specialties

    # JSON output
    python clalit.py --mode search --specialty עור --city חיפה --headless --json
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
sys.setrecursionlimit(10000)
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests
from playwright.async_api import async_playwright

# ── constants ───────────────────────────────────────────────────────────────

CLALIT_URL = "https://e-services.clalit.co.il/OnlineWeb/Services/Tamuz/TamuzTransfer.aspx"
AZCAPTCHA_DEFAULT_KEY = "8c0ea55dab3a542494b70ca16ad0b93e32357f26"

SPECIALTIES = [
    "עור", "אורתופדיה", "קרדיולוגיה", "נוירולוגיה",
    "גסטרואנטרולוגיה", "אולטרסאונד", "נפרולוגיה",
    "ראומטולוגיה", "אלרגיה", "אנדוקרינולוגיה",
    "כירורגיה", "פלסטיקה", "עיניים", "אף אוזן גרון",
    "אורולוגיה", "גריאטריה", "סוכרת", "המטולוגיה",
    "EMG", "אונקולוגיה", "בריאות האישה", "בריאות השד",
    "גריאטריה", "המטולוגיה", "יעוץ ילדים", "כבד",
    "מרפאת כאב", "ספירומטריה", "צילומי רנטגן",
    "צפיפות העצם", "רפואת להט\"ב", "תזונה קלינית",
    "פרוקטולוגיה", "ראומטולוגיה",
]

# ── data models ─────────────────────────────────────────────────────────────

@dataclass
class TimeSlot:
    time: str
    period: str

@dataclass
class DoctorSlot:
    doctor: str
    clinic: str
    address: str
    phone: str
    guid: str
    date: str
    times: list[str] = field(default_factory=list)

# ── env loader ──────────────────────────────────────────────────────────────

def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

# ── captcha ─────────────────────────────────────────────────────────────────

async def solve_captcha_azcaptcha(page, api_key: str) -> str:
    captcha_img = await page.query_selector(
        "#c_general_login_ctl00_cphbody__loginview_captchalogin_CaptchaImage"
    )
    if not captcha_img:
        return ""
    src = await captcha_img.get_attribute("src")
    if not src:
        return ""
    base_url = page.url.split("/OnlineWeb")[0]
    captcha_url = base_url + src if src.startswith("/") else src

    print(f"   Downloading captcha from {captcha_url}")
    resp = requests.get(captcha_url, timeout=30)
    resp.raise_for_status()
    image_b64 = base64.b64encode(resp.content).decode("utf-8")

    print("   Solving captcha via AzCaptcha...")
    task = requests.post(
        "https://azcaptcha.com/createTask",
        json={
            "clientKey": api_key,
            "task": {"type": "ImageToTextTask", "body": image_b64},
        },
        timeout=30,
    ).json()
    task_id = task.get("taskId")
    if not task_id:
        raise RuntimeError(f"AzCaptcha createTask failed: {task}")

    for _ in range(30):
        await asyncio.sleep(3)
        r = requests.post(
            "https://azcaptcha.com/getTaskResult",
            json={"clientKey": api_key, "taskId": task_id},
            timeout=30,
        ).json()
        if r.get("status") == "ready":
            solution = r["solution"]["text"]
            print(f"   Captcha solved: {solution}")
            return solution
    raise TimeoutError("AzCaptcha did not solve captcha in time")

# ── overlay helpers ─────────────────────────────────────────────────────────

async def remove_overlays(frame) -> None:
    await frame.evaluate("""() => {
        document.querySelectorAll('.modal, .modal-backdrop, .ui-dialog, [role="dialog"]')
            .forEach(e => { try { e.remove(); } catch(_) {} });
    }""")
    await asyncio.sleep(1.5)

# ── login ───────────────────────────────────────────────────────────────────

async def login(page, user_id: str, username: str, password: str,
                azcaptcha_key: str) -> None:
    print("1. Logging in...")
    await page.goto(CLALIT_URL, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(2)

    await page.fill("#ctl00_cphBody__loginView_tbUserId", user_id)
    await page.fill("#ctl00_cphBody__loginView_tbUserName", username)
    await page.fill("#ctl00_cphBody__loginView_tbPassword", password)

    if azcaptcha_key:
        captcha_text = await solve_captcha_azcaptcha(page, azcaptcha_key)
        if captcha_text:
            await page.evaluate("""(text) => {
                var el = document.querySelector('#ctl00_cphBody__loginView_tbCaptchaLogin');
                if (el) { el.value = text; el.dispatchEvent(new Event('input', {bubbles: true})); }
            }""", captcha_text)
            await asyncio.sleep(0.3)
            inp = await page.query_selector("#ctl00_cphBody__loginView_tbCaptchaLogin")
            if inp:
                await inp.press("Enter")

    await page.wait_for_load_state("networkidle", timeout=20000)
    await asyncio.sleep(3)
    print("   Logged in")

# ── frame helpers ───────────────────────────────────────────────────────────

def find_zimunet_frame(page):
    for fr in page.frames:
        if "Zimunet" in fr.url:
            return fr
    return None

async def navigate_to_search(page):
    print("2. Opening specialist search...")
    await page.goto(CLALIT_URL, wait_until="load", timeout=30000)
    await asyncio.sleep(8)
    f = find_zimunet_frame(page)
    if not f:
        raise RuntimeError("Tamuz iframe not found")
    await f.click("#ProfessionVisitButton")
    await asyncio.sleep(6)
    await remove_overlays(f)
    return f

# ── search form ─────────────────────────────────────────────────────────────

async def select_specialty(frame, specialty: str) -> None:
    print(f"3. Selecting specialty: {specialty}")
    await frame.select_option("#SelectedGroupCode", label=specialty)
    await asyncio.sleep(3)
    await frame.evaluate("""(spec) => {
        var sel = document.querySelector('#SelectedSpecializationCode');
        if (!sel) return;
        for (var i = 0; i < sel.options.length; i++) {
            if (sel.options[i].text.includes(spec)) {
                sel.value = sel.options[i].value;
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                return;
            }
        }
    }""", specialty)
    await asyncio.sleep(2)

async def select_city(frame, city: str) -> None:
    print(f"4. Setting city: {city}")
    ci = await frame.query_selector("#SelectedCityName")
    if not ci:
        raise RuntimeError("City input not found")
    await ci.click()
    await ci.fill("")
    await asyncio.sleep(0.5)
    await ci.type(city, delay=120)
    await asyncio.sleep(3)

    clicked = await frame.evaluate("""(city) => {
        var menu = document.querySelector('.ui-autocomplete');
        if (!menu) return 'fallback';
        var items = menu.querySelectorAll('li, .ui-menu-item');
        for (var i = 0; i < items.length; i++) {
            if (items[i].innerText.includes(city)) {
                items[i].click(); return 'clicked';
            }
        }
        return 'fallback';
    }""", city)
    if clicked == "fallback":
        await ci.press("ArrowDown")
        await asyncio.sleep(0.5)
        await ci.press("Enter")
    await asyncio.sleep(2)

    await frame.evaluate("""() => {
        var cb = document.querySelector('#IsSearchDiariesByDistricts');
        if (cb && !cb.checked) cb.click();
    }""")

async def execute_search(frame) -> list[dict]:
    print("5. Searching...")
    await frame.click("#searchBtnSpec")
    await asyncio.sleep(10)
    await remove_overlays(frame)

    cards = await frame.evaluate("""() => {
        var results = [], seen = {};
        var els = document.querySelectorAll('.doctorDetails');
        for (var i = 0; i < els.length; i++) {
            var n = els[i].innerText.trim();
            if (!n || n === '\u05de\u05d9\u05d3\u05e2 \u05e0\u05d5\u05e1\u05e3' || n.indexOf('\u05d3"') !== 0) continue;
            var card = els[i];
            for (var j = 0; j < 10; j++) {
                card = card.parentElement;
                if (!card) break;
                var btn = card.querySelector('.diaryButton[data-action-link*="AvailableVisit"]');
                if (btn) {
                    var href = btn.getAttribute('data-action-link') || '';
                    var m = href.match(/AvailableVisit\\/Index\\/([a-f0-9-]+)/);
                    if (!m) continue;
                    var guid = m[1];
                    if (seen[guid]) continue;
                    seen[guid] = true;
                    var text = card.innerText;
                    var clinic='', address='', phone='', date='';
                    var cm = text.match(/\u05de\u05e8\u05e4\u05d0\u05d4:\\s*(.+)/);
                    if (cm) clinic = cm[1].trim();
                    var am = text.match(/\u05db\u05ea\u05d5\u05d1\u05ea:\\s*(.+)/);
                    if (am) address = am[1].trim();
                    var pm = text.match(/(0[\\d-]{8,})/);
                    if (pm) phone = pm[1];
                    var dm = text.match(/\u05d1\u05ea\u05d0\u05e8\u05d9\u05da (\\d{2}\\.\\d{2}\\.\\d{4})/);
                    if (dm) date = dm[1];
                    results.push({name:n, clinic:clinic, address:address, phone:phone, date:date, guid:guid});
                    break;
                }
            }
        }
        return results;
    }""")

    text = await frame.evaluate("() => document.body.innerText")
    total = 0
    m = re.search(r"נמצאו (\d+) תוצאות", text)
    if m:
        total = int(m.group(1))

    print(f"   Found {len(cards)} doctors, {total} total results")
    return cards

# ── AvailableVisit navigation ───────────────────────────────────────────────

async def navigate_to_doctor_av(frame, guid: str) -> None:
    link = f"/Zimunet/AvailableVisit/Index/{guid}?isUpdateVisit=False"
    await frame.evaluate("(l) => { window.location.href = l; }", link)
    await asyncio.sleep(10)
    await remove_overlays(frame)

async def get_available_august_dates(frame) -> list[str]:
    return await frame.evaluate("""() => {
        var days = [];
        var cells = document.querySelectorAll('td[data-month="7"][data-year="2026"]');
        for (var i = 0; i < cells.length; i++) {
            var a = cells[i].querySelector('a');
            if (a) days.push(a.textContent.trim());
        }
        return days.sort(function(a,b){return parseInt(a)-parseInt(b);});
    }""")

async def click_calendar_date(frame, day: str, month: int = 7, year: int = 2026) -> None:
    await frame.evaluate("""(day, month, year) => {
        var cells = document.querySelectorAll('td[data-month="' + month + '"][data-year="' + year + '"]');
        for (var i = 0; i < cells.length; i++) {
            var a = cells[i].querySelector('a');
            if (a && a.textContent.trim() === day) { a.click(); return; }
        }
    }""", day, month, year)
    await asyncio.sleep(4)

async def get_times_for_selected_date(frame) -> list[str]:
    text = await frame.evaluate("() => document.body.innerText")
    times = re.findall(r'(\d{2}:\d{2})\s*\n\s*הזמן תור', text)
    return sorted(set(times))

async def get_all_doctor_slots(frame, guid: str) -> DoctorSlot:
    await navigate_to_doctor_av(frame, guid)
    text = await frame.evaluate("() => document.body.innerText")
    name_match = re.search(r'תור ל(.+?)(?: - |\n)', text)
    name = name_match.group(1).strip() if name_match else "Unknown"

    clinic_match = re.search(r'מרפאה:\s*(.+?)(?:,|$)', text)
    clinic = clinic_match.group(1).strip() if clinic_match else ""
    address_match = re.search(r'כתובת:\s*(.+?)(?:, טלפון|$)', text)
    address = address_match.group(1).strip() if address_match else ""
    phone_match = re.search(r'טלפון:\s*(0[\d\-]+)', text)
    phone = phone_match.group(1) if phone_match else ""

    dates = await get_available_august_dates(frame)
    doctor_slots = DoctorSlot(doctor=name, clinic=clinic, address=address,
                               phone=phone, guid=guid, date="")
    all_times = {}
    for day in dates:
        await click_calendar_date(frame, day)
        times = await get_times_for_selected_date(frame)
        if times:
            all_times[day] = times
    return doctor_slots, all_times

# ── booking ─────────────────────────────────────────────────────────────────

async def book_appointment(frame, guid: str, date_day: str, time_str: str,
                           month: int = 7, year: int = 2026) -> bool:
    await navigate_to_doctor_av(frame, guid)
    await click_calendar_date(frame, date_day, month, year)

    buttons = await frame.query_selector_all("a.bigButtonEnabled.createVisitButton")
    if not buttons:
        print("   No bookable buttons found")
        return False

    times_text = await frame.evaluate("""() => document.body.innerText""")
    time_lines = [l.strip() for l in times_text.split("\n")]

    time_to_button = {}
    btn_idx = 0
    for i, line in enumerate(time_lines):
        if re.match(r'^\d{2}:\d{2}$', line):
            if btn_idx < len(buttons):
                time_to_button[line] = buttons[btn_idx]
                btn_idx += 1

    if time_str not in time_to_button:
        print(f"   Time {time_str} not found among available times")
        return False

    print(f"   Clicking book for {time_str}...")
    await time_to_button[time_str].click()
    await asyncio.sleep(5)

    text = await frame.evaluate("() => document.body.innerText")
    if "התור הוזמן בהצלחה" in text:
        print("   BOOKING SUCCESSFUL!")
        date_line = re.search(r'נקבע ליום [א-ת]\'?\s*(\d{2}\.\d{2}\.\d{4})\s*בשעה\s*(\d{2}:\d{2})', text)
        if date_line:
            print(f"   Appointment: {date_line.group(1)} at {date_line.group(2)}")
        return True
    else:
        print("   Booking may have failed - checking page...")
        print(f"   Page text: {text[:300]}")
        return False

# ── find best ────────────────────────────────────────────────────────────────

def pick_best_slot(doctor_slots_map: dict[str, dict[str, list[str]]],
                   cutoff_date: str = "30.08.2026",
                   cutoff_before_hour: int = 11) -> tuple:
    cutoff = datetime.strptime(cutoff_date, "%d.%m.%Y") if cutoff_date else None
    best_date = None
    best_time = None
    best_doctor = None
    best_guid = None

    for guid, (info, date_times) in doctor_slots_map.items():
        for day_str, times in date_times.items():
            day_int = int(day_str)
            date_obj = datetime(2026, 8, day_int)
            if cutoff and date_obj >= cutoff:
                continue
            if best_date is None or day_int > int(best_date):
                best_date = day_str
                best_time = max(times)
                best_doctor = info
                best_guid = guid
            elif day_int == int(best_date) and max(times) > best_time:
                best_time = max(times)
                best_doctor = info
                best_guid = guid
    return best_guid, best_doctor, best_date, best_time

# ── search mode ─────────────────────────────────────────────────────────────

async def run_search(specialty: str, city: str, azcaptcha_key: str,
                     headless: bool = False, fetch_slots: bool = True) -> list[dict]:
    user_id = os.environ.get("CLALIT_USER_ID", "")
    username = os.environ.get("CLALIT_USERNAME", "")
    password = os.environ.get("CLALIT_PASSWORD", "")
    if not all([user_id, username, password]):
        print("Error: Set CLALIT_USER_ID, CLALIT_USERNAME, CLALIT_PASSWORD")
        sys.exit(1)

    async with async_playwright() as pw:
        b = await pw.chromium.launch(
            headless=headless,
            args=["--headless=new", "--no-sandbox"] if headless else [],
        )
        ctx = await b.new_context(viewport={"width": 1280, "height": 900}, locale="he-IL")
        page = await ctx.new_page()

        try:
            await login(page, user_id, username, password, azcaptcha_key)
            f = await navigate_to_search(page)
            await select_specialty(f, specialty)
            await select_city(f, city)
            cards = await execute_search(f)

            results = []
            for c in cards:
                entry = {k: c[k] for k in ("name", "clinic", "address", "phone", "date", "guid")}
                entry["time_slots"] = []
                if fetch_slots and c["guid"]:
                    print(f"   Checking slots for {c['name']}...")
                    try:
                        _, times = await get_all_doctor_slots(f, c["guid"])
                        entry["time_slots"] = times
                    except Exception as e:
                        print(f"     Error: {e}")
                results.append(entry)
            return results
        finally:
            await b.close()

# ── find-best mode ─────────────────────────────────────────────────────────

async def run_find_best(specialty: str, city: str, azcaptcha_key: str,
                        headless: bool = False,
                        cutoff_date: str = "30.08.2026",
                        cutoff_before_hour: int = 11) -> dict:
    user_id = os.environ.get("CLALIT_USER_ID", "")
    username = os.environ.get("CLALIT_USERNAME", "")
    password = os.environ.get("CLALIT_PASSWORD", "")
    if not all([user_id, username, password]):
        print("Error: Set CLALIT_USER_ID, CLALIT_USERNAME, CLALIT_PASSWORD")
        sys.exit(1)

    async with async_playwright() as pw:
        b = await pw.chromium.launch(
            headless=headless,
            args=["--headless=new", "--no-sandbox"] if headless else [],
        )
        ctx = await b.new_context(viewport={"width": 1280, "height": 900}, locale="he-IL")
        page = await ctx.new_page()

        try:
            await login(page, user_id, username, password, azcaptcha_key)
            f = await navigate_to_search(page)
            await select_specialty(f, specialty)
            await select_city(f, city)
            cards = await execute_search(f)

            all_slots = {}
            for c in cards:
                if c["guid"]:
                    print(f"\nChecking {c['name']}...")
                    doc_slot, times = await get_all_doctor_slots(f, c["guid"])
                    all_slots[c["guid"]] = (doc_slot, times)
                    if times:
                        for d, t in times.items():
                            print(f"  {d}.08: {', '.join(t)}")

            guid, info, best_date, best_time = pick_best_slot(all_slots, cutoff_date, cutoff_before_hour)

            if not guid:
                print("\nNo suitable appointment found")
                return {"found": False}

            print(f"\n{'='*50}")
            print(f"BEST APPOINTMENT:")
            print(f"  Doctor: {info.doctor}")
            print(f"  Clinic: {info.clinic}")
            print(f"  Address: {info.address}")
            print(f"  Date: {best_date}.08.2026")
            print(f"  Time: {best_time}")
            print(f"{'='*50}")

            print(f"\nBooking now...")
            success = await book_appointment(f, guid, best_date, best_time)
            if success:
                await page.screenshot(path="booking_confirmation.png", full_page=True)
                print("Screenshot saved to booking_confirmation.png")

            return {
                "found": True,
                "doctor": info.doctor,
                "clinic": info.clinic,
                "address": info.address,
                "date": f"{best_date}.08.2026",
                "time": best_time,
                "booked": success,
            }
        finally:
            await b.close()

# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Clalit Appointment Searcher & Booker")
    parser.add_argument("--mode", choices=["search", "find-best", "book"],
                        default="search", help="search | find-best | book")
    parser.add_argument("--specialty", "-s", default="עור", help="Specialty in Hebrew")
    parser.add_argument("--city", "-c", default="קריית טבעון", help="City in Hebrew")
    parser.add_argument("--guid", help="Doctor GUID for booking")
    parser.add_argument("--date", help="Date (DD.MM.YYYY) for booking")
    parser.add_argument("--time", help="Time (HH:MM) for booking")
    parser.add_argument("--headless", action="store_true", help="Run headless")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--no-slots", action="store_true", help="Skip fetching slots")
    parser.add_argument("--azcaptcha-key", help="AzCaptcha API key")
    parser.add_argument("--env", default=".env", help="Env file path")
    parser.add_argument("--list-specialties", action="store_true", help="List specialties")
    args = parser.parse_args()

    load_env(args.env)
    azcaptcha_key = args.azcaptcha_key or os.environ.get("AZCAPTCHA_API_KEY") or AZCAPTCHA_DEFAULT_KEY

    if args.list_specialties:
        print("Available specialties:")
        for s in SPECIALTIES:
            print(f"  {s}")
        return

    if args.mode == "search":
        results = asyncio.run(run_search(
            args.specialty, args.city, azcaptcha_key,
            args.headless, not args.no_slots,
        ))
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            for r in results:
                print(f"\n{r['name']}")
                print(f"  מרפאה: {r['clinic']}")
                print(f"  כתובת: {r['address']}")
                print(f"  טלפון: {r['phone']}")
                print(f"  תאריך: {r['date']}")
                if isinstance(r.get("time_slots"), dict) and r["time_slots"]:
                    for d, t in r["time_slots"].items():
                        print(f"  {d}.08: {', '.join(t)}")

    elif args.mode == "find-best":
        result = asyncio.run(run_find_best(
            args.specialty, args.city, azcaptcha_key, args.headless,
        ))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.mode == "book":
        if not all([args.guid, args.date, args.time]):
            print("Error: --guid, --date, and --time required for book mode")
            sys.exit(1)

        async def do_book():
            user_id = os.environ.get("CLALIT_USER_ID", "")
            username = os.environ.get("CLALIT_USERNAME", "")
            password = os.environ.get("CLALIT_PASSWORD", "")
            if not all([user_id, username, password]):
                print("Error: Credentials not set"); sys.exit(1)

            async with async_playwright() as pw:
                b = await pw.chromium.launch(
                    headless=args.headless,
                    args=["--headless=new", "--no-sandbox"] if args.headless else [],
                )
                ctx = await b.new_context(viewport={"width": 1280, "height": 900}, locale="he-IL")
                page = await ctx.new_page()
                try:
                    await login(page, user_id, username, password, azcaptcha_key)
                    f = await navigate_to_search(page)
                    date_parts = args.date.split(".")
                    day = date_parts[0].lstrip("0")
                    success = await book_appointment(f, args.guid, day, args.time)
                    if success:
                        await page.screenshot(path="booking_confirmation.png", full_page=True)
                    return {"success": success}

                finally:
                    await b.close()

        result = asyncio.run(do_book())
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()

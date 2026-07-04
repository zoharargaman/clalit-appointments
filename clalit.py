#!/usr/bin/env python3
"""
Clalit Healthcare Appointment Scraper

Searches for available specialist appointments on the Clalit
healthcare system (Israel) near specified cities.

Usage:
    python clalit.py --specialty עור --city "קריית טבעון"
    python clalit.py --specialty עור --city חיפה --headless
    python clalit.py --list-specialties
"""

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import async_playwright

CLALIT_URL = "https://e-services.clalit.co.il/OnlineWeb/Services/Tamuz/TamuzTransfer.aspx"


@dataclass
class Appointment:
    doctor: str
    specialty: str
    date: str
    clinic: str
    address: str
    phone: str

    def __str__(self):
        return (
            f"{self.doctor}\n"
            f"  תאריך: {self.date}\n"
            f"  מרפאה: {self.clinic}\n"
            f"  כתובת: {self.address}\n"
            f"  טלפון: {self.phone}"
        )


@dataclass
class SearchResults:
    city: str
    specialty: str
    total: int
    appointments: list[Appointment] = field(default_factory=list)


async def login(page, user_id: str, username: str, password: str):
    """Log in to Clalit e-services."""
    print("1. Logging in...")
    await page.goto(CLALIT_URL, wait_until="networkidle", timeout=30000)
    await page.fill("#ctl00_cphBody__loginView_tbUserId", user_id)
    await page.fill("#ctl00_cphBody__loginView_tbUserName", username)
    await page.fill("#ctl00_cphBody__loginView_tbPassword", password)

    await page.evaluate("""() => {
        for (const el of document.querySelectorAll('*')) {
            if (el.innerText?.trim() === 'כניסה' && el.offsetParent) {
                el.click();
                return;
            }
        }
    }""")

    await page.wait_for_load_state("networkidle", timeout=20000)
    await asyncio.sleep(3)
    print(f"   Logged in: {page.url}")


async def navigate_to_specialist_form(page):
    """Navigate to the specialist appointment search form."""
    print("2. Opening specialist appointment form...")
    await page.goto(CLALIT_URL, wait_until="load", timeout=30000)
    await asyncio.sleep(8)

    # Find the Tamuz iframe
    tamuz_frame = None
    for frame in page.frames:
        if "Zimunet" in frame.url:
            tamuz_frame = frame
            break

    if not tamuz_frame:
        raise RuntimeError("Could not find Tamuz iframe")

    # Click "לרפואה יועצת" (specialist consultation)
    await tamuz_frame.click("#ProfessionVisitButton")
    await asyncio.sleep(5)
    print("   Form ready")
    return tamuz_frame


async def select_specialty(frame, specialty: str):
    """Select medical specialty from dropdowns."""
    print(f"3. Selecting specialty: {specialty}...")
    await frame.select_option("#SelectedGroupCode", label=specialty)
    await asyncio.sleep(3)

    # Select same value in specialization dropdown via JS
    await frame.evaluate("""(spec) => {
        const sel = document.querySelector('#SelectedSpecializationCode');
        if (!sel) return;
        for (const o of sel.options) {
            if (o.text.includes(spec)) {
                sel.value = o.value;
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                return;
            }
        }
    }""", specialty)
    await asyncio.sleep(2)


async def select_city(frame, city: str):
    """Type city name and select from autocomplete."""
    print(f"4. Setting city: {city}...")
    city_input = await frame.query_selector("#SelectedCityName")
    if not city_input:
        raise RuntimeError("City input field not found")

    await city_input.click()
    await city_input.fill("")
    await asyncio.sleep(0.5)
    await city_input.type(city, delay=150)
    await asyncio.sleep(3)

    # Try clicking matching autocomplete suggestion
    clicked = await frame.evaluate("""(city) => {
        const menu = document.querySelector('.ui-autocomplete');
        if (!menu) return 'no menu';
        const items = menu.querySelectorAll('li, .ui-menu-item');
        for (const item of items) {
            if (item.innerText.includes(city)) {
                item.click();
                return 'clicked: ' + item.innerText.trim();
            }
        }
        // Fallback: ArrowDown + Enter
        return 'fallback';
    }""", city)

    if clicked == "fallback":
        await city_input.press("ArrowDown")
        await asyncio.sleep(0.5)
        await city_input.press("Enter")
    else:
        print(f"   {clicked}")

    await asyncio.sleep(2)

    # Enable "include nearby settlements"
    await frame.evaluate("""() => {
        const cb = document.querySelector('#IsSearchDiariesByDistricts');
        if (cb && !cb.checked) cb.click();
    }""")


async def search(frame) -> str:
    """Click search and return raw results text."""
    print("5. Searching...")
    await frame.click("#searchBtnSpec")
    await asyncio.sleep(10)
    return await frame.evaluate(
        "() => document.body ? document.body.innerText.substring(0, 30000) : ''"
    )


def parse_results(text: str, city: str, specialty: str) -> SearchResults:
    """Parse the raw page text into structured results."""
    # Extract total count
    total = 0
    m = re.search(r"נמצאו (\d+) תוצאות", text)
    if m:
        total = int(m.group(1))

    results = SearchResults(city=city, specialty=specialty, total=total)

    # Split by doctor blocks - each starts with "ד"ר"
    blocks = re.split(r"(?=ד\"ר )", text)
    for block in blocks:
        if not block.startswith("ד\"ר"):
            continue

        lines = block.strip().split("\n")
        doctor = lines[0].strip() if lines else ""

        date = ""
        clinic = ""
        address = ""
        phone = ""

        for line in lines:
            line = line.strip()
            if "התור הפנוי הקרוב" in line:
                m2 = re.search(r"בתאריך (\d{2}\.\d{2}\.\d{4})", line)
                if m2:
                    date = m2.group(1)
            elif line.startswith("מרפאה:"):
                clinic = line.split(":", 1)[1].strip()
            elif line.startswith("כתובת:"):
                address = line.split(":", 1)[1].strip()
            elif re.match(r"^0[\d-]{8,}$", line):
                phone = line

        if doctor and date:
            results.appointments.append(
                Appointment(
                    doctor=doctor,
                    specialty=specialty,
                    date=date,
                    clinic=clinic,
                    address=address,
                    phone=phone,
                )
            )

    return results


async def run_search(
    specialty: str,
    city: str,
    headless: bool = False,
) -> SearchResults:
    """Full flow: login, navigate, search, parse."""
    user_id = os.environ.get("CLALIT_USER_ID", "")
    username = os.environ.get("CLALIT_USERNAME", "")
    password = os.environ.get("CLALIT_PASSWORD", "")

    if not all([user_id, username, password]):
        print("Error: Set CLALIT_USER_ID, CLALIT_USERNAME, CLALIT_PASSWORD")
        print("in environment or .env file")
        sys.exit(1)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--headless=new", "--no-sandbox"] if headless else [],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="he-IL",
        )
        page = await context.new_page()

        try:
            await login(page, user_id, username, password)
            frame = await navigate_to_specialist_form(page)
            await select_specialty(frame, specialty)
            await select_city(frame, city)
            raw_text = await search(frame)
            return parse_results(raw_text, city, specialty)
        finally:
            await browser.close()


def load_env(path: str = ".env"):
    """Load .env file into os.environ."""
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


def main():
    parser = argparse.ArgumentParser(
        description="Search Clalit healthcare appointments"
    )
    parser.add_argument(
        "--specialty", "-s",
        default="עור",
        help="Medical specialty in Hebrew (default: עור/dermatology)",
    )
    parser.add_argument(
        "--city", "-c",
        default="קריית טבעון",
        help="City name in Hebrew (default: קריית טבעון)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--list-specialties",
        action="store_true",
        help="List available specialties and exit",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to .env file (default: .env)",
    )

    args = parser.parse_args()
    load_env(args.env)

    if args.list_specialties:
        print("Common specialties ( Hebrew ):")
        specs = [
            "עור", "אורתופדיה", "קרדיולוגיה", "נוירולוגיה",
            "גסטרואנטרולוגיה", "אולטרסאונד", "נפרולוגיה",
            "ראומטולוגיה", "אלרגיה", "אנדוקרינולוגיה",
            "כירורגיה", "פלסטיקה", "עיניים", "אף אוזן גרון",
            "אורולוגיה", "גריאטריה", "סוכרת", "המטולוגיה",
            "訾nutrition קלינית", "רפואת להט\"ב",
        ]
        for s in specs:
            print(f"  {s}")
        return

    results = asyncio.run(
        run_search(args.specialty, args.city, args.headless)
    )

    if args.json:
        data = {
            "city": results.city,
            "specialty": results.specialty,
            "total": results.total,
            "appointments": [
                {
                    "doctor": a.doctor,
                    "date": a.date,
                    "clinic": a.clinic,
                    "address": a.address,
                    "phone": a.phone,
                }
                for a in results.appointments
            ],
        }
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'='*50}")
        print(f"תוצאות: {results.total} תורים")
        print(f"תחום: {results.specialty}")
        print(f"עיר: {results.city}")
        print(f"{'='*50}\n")

        if not results.appointments:
            print("לא נמצאו תורים")
            return

        for i, apt in enumerate(results.appointments, 1):
            print(f"{i}. {apt}\n")


if __name__ == "__main__":
    main()

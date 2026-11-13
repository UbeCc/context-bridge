#!/usr/bin/env python3
"""Capture the console screenshots used as PR evidence and verify the UI end-to-end.

Run against the **synthetic demo hub** served by the real API — never against a live hub with real
bot data, because these images are committed to a public repository:

    python3 scripts/make-demo-hub.py --out /tmp/demo-hub
    python3 -m context_bridge.cli --config /tmp/demo-hub/demo.toml serve --port 18098
    python3 web/dev/capture-screenshots.py --base http://localhost:18098

It writes ``docs/screenshots/after.png`` (1440x900) and ``docs/screenshots/after-mobile.png``
(390x844), and exercises the key flows: route sweep, board create/move/delete, memory search and
the record drawer, and the console dry-run state. Exits non-zero on console errors, mobile
horizontal overflow or a failed flow.

Requires Playwright with a cached Chromium (development-only dependency; the console itself has no
build step and no runtime dependencies).

before.png predates the polished theme and is kept from the first functional plain iteration.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SHOTS = REPO_ROOT / "docs" / "screenshots"
ROUTES = ["overview", "bots", "memory", "board", "console", "about"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://localhost:18098", help="console base URL (demo hub server)")
    parser.add_argument("--outdir", type=Path, default=DEFAULT_SHOTS, help="directory for the committed screenshots")
    parser.add_argument("--scratch", type=Path, default=None, help="directory for extra verification screenshots")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    outdir = args.outdir.resolve()
    scratch = (args.scratch or Path("/tmp/context-bridge-shots")).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    report: dict[str, object] = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        # ---- desktop ---------------------------------------------------------
        desktop = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page = desktop.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(f"{args.base}/", wait_until="networkidle")
        page.wait_for_timeout(1500)
        theme = page.evaluate("document.documentElement.dataset.theme")
        report["theme"] = theme
        if theme != "dark":
            problems.append(f"default theme is {theme!r}, expected dark")
        if page.locator(".sidebar .brand-close").is_visible():
            problems.append("desktop shows the mobile close button in the sidebar")
        if page.locator(".topbar .nav-open").is_visible():
            problems.append("desktop shows the mobile hamburger button")

        first_bot = None
        for route in ROUTES:
            errors.clear()
            page.goto(f"{args.base}/#/{route}")
            page.wait_for_timeout(1400)
            text = page.locator("#view").inner_text()
            report[f"view_{route}_chars"] = len(text)
            if errors:
                problems.append(f"console errors on {route}: {errors}")
            if len(text) < 40:
                problems.append(f"{route} looks empty")
            if route == "bots":
                first_bot = page.locator(".bot-card").first.get_attribute("data-bot-id")
        if first_bot:
            page.goto(f"{args.base}/#/bots/{first_bot}")
            page.wait_for_timeout(1700)
            report["digest_chars"] = len(page.locator("#digest-body").inner_text())
            if report["digest_chars"] < 20:
                problems.append("bot detail digest looks empty")
            page.screenshot(path=str(scratch / "bot-detail.png"))

        # committed desktop screenshot: overview, pristine top of page
        page.goto(f"{args.base}/#/overview")
        page.wait_for_timeout(1700)
        page.screenshot(path=str(outdir / "after.png"))
        report["after_bytes"] = (outdir / "after.png").stat().st_size

        # ---- board: create -> move -> delete --------------------------------
        page.goto(f"{args.base}/#/board")
        page.wait_for_timeout(1300)
        before_cards = page.locator(".kanban-card").count()
        page.locator('[data-action="card-new"]').first.click()
        page.wait_for_timeout(450)
        page.fill('#card-form input[name="title"]', "Synthetic verification card")
        page.fill('#card-form textarea[name="desc"]', "Created by capture-screenshots.py; removed again.")
        assignees = page.eval_on_selector(
            '#card-form select[name="assignee"]',
            "el => Array.from(el.options).map(o => o.value).filter(Boolean)",
        )
        if assignees:
            page.select_option('#card-form select[name="assignee"]', assignees[0])
        page.click('#card-form button[type="submit"]')
        page.wait_for_timeout(1500)
        created = page.locator(".kanban-card").count()
        report["board"] = {"before": before_cards, "after_create": created}
        if created != before_cards + 1:
            problems.append("board card create failed")
        card = page.locator(".kanban-card", has_text="Synthetic verification card").first
        card.locator('[data-action="card-move"]').last.click()
        page.wait_for_timeout(1500)
        columns = page.locator('.board-col[data-column="in_progress"] .kanban-card', has_text="Synthetic verification card").count()
        if not columns:
            problems.append("board card move failed")
        page.locator(".kanban-card", has_text="Synthetic verification card").first.locator(".card-edit").click(force=True)
        page.wait_for_timeout(450)
        page.click('[data-action="card-delete"]')
        page.wait_for_timeout(400)
        page.locator('[data-confirm]').click()
        page.wait_for_timeout(1500)
        if page.locator(".kanban-card", has_text="Synthetic verification card").count():
            problems.append("board card delete failed")
        page.screenshot(path=str(scratch / "board.png"))

        # ---- memory search + record drawer ---------------------------------
        page.goto(f"{args.base}/#/memory")
        page.wait_for_timeout(1200)
        page.fill("#memory-search", "screenshot")
        page.wait_for_timeout(1300)
        hits = page.locator(".search-result").count()
        report["search_hits"] = hits
        if not hits:
            problems.append("memory search returned no hits (demo hub should contain 'screenshot')")
        else:
            page.locator(".search-result").first.click()
            page.wait_for_timeout(1200)
            if not page.locator(".overlay-drawer .record-detail").count():
                problems.append("record drawer did not open")
            page.screenshot(path=str(scratch / "record-drawer.png"))
            page.keyboard.press("Escape")

        # ---- console dry-run / relay-disabled state ------------------------
        page.goto(f"{args.base}/#/console")
        page.wait_for_timeout(1100)
        page.fill("#console-text", "dry-run verification, do not relay")
        page.click(".console-send")
        page.wait_for_timeout(1500)
        result = page.locator("#console-result").inner_text()
        report["console_result"] = result.replace("\n", " | ")[:160]
        if "已跳过" not in result or "dry-run" not in result:
            problems.append("console does not explain the skipped/dry-run state")
        desktop.close()

        # ---- mobile ---------------------------------------------------------
        mobile = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=1)
        mpage = mobile.new_page()
        mobile_errors: list[str] = []
        mpage.on("console", lambda m: mobile_errors.append(m.text) if m.type == "error" else None)
        mpage.on("pageerror", lambda e: mobile_errors.append(str(e)))
        mpage.goto(f"{args.base}/", wait_until="networkidle")
        mpage.wait_for_timeout(1600)
        overflow = mpage.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        report["mobile_overflow_px"] = overflow
        if overflow > 1:
            problems.append(f"mobile horizontal overflow of {overflow}px")
        if not mpage.locator(".bottom-nav").is_visible():
            problems.append("mobile bottom navigation is hidden")
        if mobile_errors:
            problems.append(f"mobile console errors: {mobile_errors}")
        mpage.screenshot(path=str(outdir / "after-mobile.png"))
        report["after_mobile_bytes"] = (outdir / "after-mobile.png").stat().st_size
        mpage.locator('.bottom-item[data-route="board"]').click()
        mpage.wait_for_timeout(1500)
        mpage.screenshot(path=str(scratch / "mobile-board.png"))
        mobile.close()
        browser.close()

    for name in ("after.png", "after-mobile.png"):
        size = (outdir / name).stat().st_size
        if size > 500 * 1024:
            problems.append(f"{name} is {size} bytes (>500KB)")

    report["problems"] = problems
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if problems:
        print("\nPROBLEMS:", file=sys.stderr)
        for item in problems:
            print(" -", item, file=sys.stderr)
        return 1
    print("\nScreenshots captured and all UI checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

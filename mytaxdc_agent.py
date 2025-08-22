import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from browser_use import Agent, Controller, ActionResult, ChatOpenAI, BrowserProfile, BrowserSession
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

controller = Controller()


@controller.action("Run DC Clean Hands workflow deterministically")
async def clean_hands_workflow(
    notice: str,
    last4: str,
    browser_session: BrowserSession,
) -> ActionResult:
    """
    Deterministic site-specific workflow for DC MyTax Clean Hands:
    1) Navigate to https://mytax.dc.gov/_/
    2) Click "Validate a Certificate of Clean Hands"
    3) Fill Notice Number 
    4) Fill Last 4 digits
    4) Click Search and capture compliance status
    5) Request a current certificate, (compliant or noncompliant) click Next, Submit
    7) Click View Certificate (PDF) and save it under artifacts/
    Returns ActionResult with JSON payload and is_done=True
    """
    result: dict = {
        "status": "unknown",
        "message": "",
        "screenshot_path": None,
        "pdf_path": None,
        "urls": [],
        "notice": notice,
        "last4": last4,
    }

    ts = int(time.time())

    def add_url(u: Optional[str]):
        if u:
            result["urls"].append(u)

    # Resolve current page from session
    page = await browser_session.get_current_page()

    # Step 1: open site
    await page.goto("https://mytax.dc.gov/_/", wait_until="domcontentloaded", timeout=120_000)
    add_url(page.url)

    # Step 2: click Validate a Certificate of Clean Hands
    try:
        link = page.get_by_role("link", name=re.compile(r"Validate.*Clean\s*Hands", re.I))
        if await link.count() == 0:
            link = page.get_by_text(re.compile(r"Validate a Certificate of Clean Hands", re.I))
        await link.first.click(timeout=30_000)
    except Exception:
        # Try scrolling then clicking again
        try:
            await page.mouse.wheel(0, 2000)
            link = page.get_by_role("link", name=re.compile(r"Validate.*Clean\s*Hands", re.I))
            await link.first.click(timeout=30_000)
        except Exception as e:
            msg = f"Failed to find/click 'Validate a Certificate of Clean Hands': {type(e).__name__}: {e}"
            return ActionResult(error=msg)

    await page.wait_for_load_state("domcontentloaded")
    add_url(page.url)

    # Step 3: fill notice and last4 by clicking fields
    try:
        # Enter notice number in first field
        await page.locator("input").nth(0).fill(notice, timeout=10_000)
        
        # Click into the L4 field (second input) and enter L4 digits
        await page.locator("input").nth(1).click(timeout=10_000)
        await page.locator("input").nth(1).fill(last4, timeout=10_000)
        
        # Click the search button
        try:
            btn = page.get_by_role("button", name=re.compile(r"^Search$", re.I))
            if await btn.count() == 0:
                btn = page.locator('button:has-text("Search"), input[type="submit"][value*="Search" i]')
            await btn.first.click(timeout=20_000)
        except Exception:
            # Fallback: try pressing Enter on the last input field
            await page.locator("input").nth(1).press("Enter")
        
    except Exception as e:
        return ActionResult(error=f"Failed to fill form: {type(e).__name__}: {e}")

    # Step 5: capture result status and screenshot
    await page.wait_for_timeout(2000)
    try:
        body_text = await page.text_content("body")
    except Exception:
        body_text = None

    status = "unknown"
    if body_text:
        if re.search(r"\bnon[- ]?compliant\b", body_text, re.I):
            status = "noncompliant"
        elif re.search(r"\bcompliant\b", body_text, re.I):
            status = "compliant"

    result["status"] = status
    result["message"] = (
        "Detected compliance status from page." if status != "unknown" else "Could not detect compliance status."
    )

    # Screenshot removed to focus on PDF generation

    # Step 7-10: request current certificate by clicking elements
    try:
        # Click the compliance request link
        req_link = page.get_by_role("link", name=re.compile(r"Click here to request.*Certificate of Clean Hands", re.I))
        if await req_link.count() == 0:
            req_link = page.get_by_text(re.compile(r"Click here to request a current Certificate of Clean Hands", re.I), exact=False)
        
        if await req_link.count() > 0:
            await req_link.first.click(timeout=30_000)
            await page.wait_for_load_state("domcontentloaded")
            add_url(page.url)

            # Wait a moment for page to load
            await page.wait_for_timeout(2000)
            
            # Click Next button
            next_btn = page.get_by_role("button", name=re.compile(r"^Next$", re.I))
            if await next_btn.count() > 0:
                await next_btn.first.click(timeout=20_000)
                await page.wait_for_load_state("domcontentloaded")
                add_url(page.url)

            # Wait a moment for page to load  
            await page.wait_for_timeout(2000)
            
            # Click Submit button
            submit_btn = page.get_by_role("button", name=re.compile(r"Submit", re.I))
            if await submit_btn.count() > 0:
                await submit_btn.first.click(timeout=30_000)
                await page.wait_for_load_state("domcontentloaded")
                add_url(page.url)

        # Wait a moment for page to load
        await page.wait_for_timeout(1000)
        
        # View certificate (PDF) - look for view certificate link
        view_link = page.get_by_role("link", name=re.compile(r"view certificate", re.I))
        if await view_link.count() == 0:
            view_link = page.get_by_text(re.compile(r"view certificate", re.I), exact=False)

        pdf_path: Optional[str] = None
        if await view_link.count() > 0:
            # First try a direct download event
            try:
                async with page.expect_download(timeout=20_000) as dl_info:
                    await view_link.first.click()
                download = await dl_info.value
                pdf_path = str(ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}.pdf")
                await download.save_as(pdf_path)
            except Exception:
                # Fallback: sometimes it opens a new tab
                try:
                    popup = await browser_session.browser_context.wait_for_event("page", timeout=10_000)
                    await popup.wait_for_load_state("domcontentloaded")
                    add_url(popup.url)
                    # Fetch PDF bytes within page context to preserve session
                    bytes_list = await popup.evaluate(
                        """
                        async () => {
                            const res = await fetch(window.location.href);
                            const ab = await res.arrayBuffer();
                            return Array.from(new Uint8Array(ab));
                        }
                        """
                    )
                    if bytes_list:
                        pdf_path = str(ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}.pdf")
                        with open(pdf_path, "wb") as f:
                            f.write(bytes(bytes_list))
                except Exception:
                    pass

            if pdf_path:
                result["pdf_path"] = pdf_path
    except Exception:
        # Non-fatal: workflow may vary
        pass

    extracted = json.dumps(result)
    return ActionResult(
        extracted_content=extracted,
        is_done=True,
    )


async def main():
    load_dotenv()

    notice = os.getenv("NOTICE", "L0014500721")
    last4 = os.getenv("L4", "0257")
    model_name = os.getenv("MODEL_NAME", "gpt-4.1-mini")

    llm = ChatOpenAI(model=model_name)

    # Configure browser (headless=False for visibility while developing)
    browser_profile = BrowserProfile(headless=False, downloads_path=str(ARTIFACTS_DIR))
    browser_session = BrowserSession(browser_profile=browser_profile)

    # Run our custom action as an initial action (no LLM control)
    initial_actions = [
        {"clean_hands_workflow": {"notice": notice, "last4": last4}},
    ]

    agent = Agent(
        task=f"Validate DC Clean Hands for notice {notice} and request current certificate.",
        llm=llm,
        controller=controller,
        browser_session=browser_session,
        initial_actions=initial_actions,
    )

    history = await agent.run(max_steps=50)
    print("\n-- Agent run complete --")
    print("Visited URLs:", history.urls())

    final = history.final_result()
    if final:
        print("Final Result JSON:\n", final)
    else:
        print("No final result returned.")


if __name__ == "__main__":
    asyncio.run(main())

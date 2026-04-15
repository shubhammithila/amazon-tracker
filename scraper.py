import asyncio
import random
import re
from playwright.async_api import async_playwright

try:
    from playwright_stealth import stealth_async
except ImportError:
    async def stealth_async(page): pass

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 800},
]

CONCURRENCY = 5    # 5 parallel — safe memory ceiling on Railway (~2.5 GB peak)
PINCODE = "400076"


async def safe_text(element):
    try:
        return (await element.inner_text()).strip()
    except:
        return ""


async def safe_attr(element, attr):
    try:
        return (await element.get_attribute(attr) or "").strip()
    except:
        return ""


async def configure_page(page):
    """Apply stealth and block images/fonts/media so pages load faster."""
    await stealth_async(page)
    # Abort heavy resources — product data is in HTML/JS, not images or fonts
    await page.route(
        re.compile(r'\.(png|jpe?g|gif|webp|svg|ico|woff2?|ttf|eot|mp4|mp3|pdf)(\?.*)?$', re.IGNORECASE),
        lambda route: route.abort()
    )
    # Block known ad/tracking endpoints that add latency but carry no product data
    await page.route(
        re.compile(r'(doubleclick\.net|google-analytics|amazon-adsystem|fls-na\.amazon|mads\.amazon)'),
        lambda route: route.abort()
    )


async def set_pincode(page):
    """
    Visit amazon.in and set the delivery pincode via the location widget.
    Must be called once per page/context before scraping product pages.
    """
    try:
        await page.goto("https://www.amazon.in", wait_until="domcontentloaded", timeout=12000)
        await asyncio.sleep(random.uniform(1, 2))

        # Click the "Deliver to" location widget
        for sel in ["#nav-global-location-popover-link", "#glow-ingress-block", "#glow-ingress-line2"]:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                break

        # Wait for the pincode input to appear in the popup
        try:
            await page.wait_for_selector("#GLUXZipUpdateInput", timeout=5000)
        except:
            return  # Popup didn't open — skip, scrape will still run without pincode

        zip_input = await page.query_selector("#GLUXZipUpdateInput")
        if zip_input:
            await zip_input.click(click_count=3)   # select any existing value
            await zip_input.fill(PINCODE)
            await asyncio.sleep(0.5)

        # Click the Apply / Submit button
        for sel in ["#GLUXZipUpdate input[type='submit']", "#GLUXZipUpdate", "span#GLUXZipUpdate input"]:
            apply_btn = await page.query_selector(sel)
            if apply_btn:
                await apply_btn.click()
                break

        await asyncio.sleep(1.5)   # let the page reload the location
    except:
        pass   # Never block scraping because of pincode failure


async def scrape_asin(page, asin):
    url = f"https://www.amazon.in/dp/{asin}"
    result = {
        "ASIN": asin,
        "URL": url,
        "Title": "",
        "Rating": "",
        "No. of Ratings": "",
        "BSR": "",
        "Buybox Price": "",
        "Buybox Seller": "",
        "Buybox Fulfillment": "",
        "Other Sellers": "",
        "Limited Time Deal": "No",
        "Use By Date": "",
        "Status": "OK",
    }

    try:
        # ── Navigate with retry on ERR_ABORTED ───────────────────────────────
        for attempt in range(3):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                break  # success — continue to scraping
            except Exception as nav_err:
                if attempt == 2:
                    result["Status"] = f"Error: {str(nav_err)[:80]}"
                    return result
                # ERR_ABORTED / network errors — back off and re-warm session
                backoff = 10 + attempt * 10   # 10 s, then 20 s
                await asyncio.sleep(backoff)
                try:
                    await page.goto("https://www.amazon.in",
                                    wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(random.uniform(2, 3))
                except Exception:
                    pass

        await asyncio.sleep(random.uniform(1, 2))

        # Check for captcha
        body_text = await page.inner_text("body")
        if "Enter the characters you see below" in body_text or "Type the characters" in body_text:
            result["Status"] = "Blocked (CAPTCHA)"
            return result

        # ── Title ────────────────────────────────────────────────────────────
        title_el = await page.query_selector("#productTitle")
        if title_el:
            result["Title"] = await safe_text(title_el)

        # ── Rating ───────────────────────────────────────────────────────────
        for sel in ["#acrPopover", "#averageCustomerReviews #acrPopover"]:
            el = await page.query_selector(sel)
            if el:
                title_attr = await safe_attr(el, "title")
                if title_attr:
                    result["Rating"] = title_attr.split(" ")[0]
                    break

        # ── Number of ratings ─────────────────────────────────────────────────
        for sel in ["#acrCustomerReviewText", "#acrCustomerReviewLink span"]:
            el = await page.query_selector(sel)
            if el:
                result["No. of Ratings"] = await safe_text(el)
                break

        # ── BSR (Best Seller Rank) ────────────────────────────────────────────
        try:
            bsr_found = False
            # Method 1: #SalesRank element
            bsr_el = await page.query_selector("#SalesRank")
            if bsr_el:
                bsr_text = await safe_text(bsr_el)
                m = re.search(r'#[\d,]+\s+in\s+[^\n(]+', bsr_text)
                if m:
                    result["BSR"] = m.group(0).strip()
                    bsr_found = True

            # Method 2: product details table rows / bullets
            if not bsr_found:
                detail_sels = [
                    "#productDetails_techSpec_section_1 tr",
                    "#productDetails_detailBullets_sections1 tr",
                    ".prodDetTable tr",
                    "#detailBullets_feature_div li",
                    "#productDetails_db_sections tr",
                ]
                for sel in detail_sels:
                    rows = await page.query_selector_all(sel)
                    for row in rows:
                        row_text = await safe_text(row)
                        if "best sellers rank" in row_text.lower() or "amazon bestsellers rank" in row_text.lower():
                            m = re.search(r'#([\d,]+)\s+in\s+([^\n(]+)', row_text)
                            if m:
                                result["BSR"] = f"#{m.group(1)} in {m.group(2).strip()}"
                            bsr_found = True
                            break
                    if bsr_found:
                        break
        except:
            pass

        # ── Buybox price ──────────────────────────────────────────────────────
        for sel in [
            ".priceToPay span.a-price-whole",
            "#corePrice_feature_div .a-price-whole",
            "#price_inside_buybox",
            "#apex_offerDisplay_desktop .priceToPay span.a-price-whole",
            "#corePrice_desktop .a-price-whole",
        ]:
            el = await page.query_selector(sel)
            if el:
                price = await safe_text(el)
                if price:
                    result["Buybox Price"] = "₹" + price.replace(",", "").replace(".", "").strip()
                    break

        # ── Buybox seller ─────────────────────────────────────────────────────
        for sel in [
            "#sellerProfileTriggerId",
            "#tabular-buybox #sellerProfileTriggerId",
            "#merchantInfoFeature_feature_div #sellerProfileTriggerId",
            "#merchant-info a",
        ]:
            el = await page.query_selector(sel)
            if el:
                name = await safe_text(el)
                if name:
                    result["Buybox Seller"] = name
                    break

        # ── Buybox fulfillment ────────────────────────────────────────────────
        # "Ships from Amazon"  → FBA   |   "Delivered by Amazon" → Easy Ship
        #
        # Root cause of previous failures:
        #   amazon.in buybox is an HTML <table>. innerText separates cells
        #   with tabs (\t), not newlines (\n). So "Ships from" and "Amazon"
        #   arrive as ONE token "ships from\tamazon" when splitting by \n only.
        #
        # Fix: split by /[\n\t]+/ to tokenise both table cells and div lines.
        # Also try a proximity-regex on the raw text as a belt-and-braces fallback.
        try:
            fulfillment = await page.evaluate("""() => {
                const areas = [
                    '#tabular-buybox',
                    '#apex_offerDisplay_desktop',
                    '#merchant-info',
                    '#tabular-buybox-container',
                    '#desktop_buybox_group',
                    '#buybox',
                    '#centerCol'
                ];
                for (const sel of areas) {
                    const el = document.querySelector(sel);
                    if (!el) continue;
                    const raw = el.innerText.toLowerCase();

                    // ── Pass 1: tokenise by BOTH newline AND tab ─────────────
                    // Handles table cells (tab-separated) and div-based layouts
                    const parts = raw.split(/[\\n\\t]+/).map(s => s.trim()).filter(Boolean);
                    for (let i = 0; i < parts.length; i++) {
                        const cur = parts[i];
                        const nxt = parts[i + 1] || '';
                        if (cur === 'ships from'   && nxt.includes('amazon')) return 'FBA';
                        if (cur === 'delivered by' && nxt.includes('amazon')) return 'Easy Ship';
                        // same token (e.g. "ships from amazon.in")
                        if (cur.startsWith('ships from')   && cur.includes('amazon')) return 'FBA';
                        if (cur.startsWith('delivered by') && cur.includes('amazon')) return 'Easy Ship';
                    }

                    // ── Pass 2: proximity regex on raw text ──────────────────
                    // Catches any remaining whitespace/punctuation variants
                    if (/ships from[\\s\\S]{0,30}amazon/.test(raw))   return 'FBA';
                    if (/delivered by[\\s\\S]{0,30}amazon/.test(raw)) return 'Easy Ship';
                }
                return '';
            }""")
            if fulfillment:
                result["Buybox Fulfillment"] = fulfillment
        except:
            pass

        # ── Limited time deal ─────────────────────────────────────────────────
        # Only check exact deal badge elements — no broad/text selectors
        for sel in ["#dealBadge", "#dealnudge", "#limitedTimeDealBadge", ".dealBadge"]:
            el = await page.query_selector(sel)
            if el:
                # Confirm the element is visible and has content
                visible = await el.is_visible()
                if visible:
                    result["Limited Time Deal"] = "Yes"
                    break

        # ── Use By Date ───────────────────────────────────────────────────────
        try:
            center_el = await page.query_selector("#centerCol, #ppd")
            if center_el:
                center_text = await safe_text(center_el)
                m = re.search(
                    r'(?:Use by|Best before|Best By|Expiry date|Expiry|Expires)[:\s]+([^\n]{4,25})',
                    center_text, re.IGNORECASE
                )
                if m:
                    result["Use By Date"] = m.group(1).strip()
        except:
            pass

        # ── Other sellers (AOD panel) ─────────────────────────────────────────
        try:
            see_all_btn = None
            for sel in [
                "#buybox-see-all-buying-choices-announce",
                "#aod-ingress-link",
                "a[href*='#buying-choices']",
            ]:
                see_all_btn = await page.query_selector(sel)
                if see_all_btn:
                    break

            if see_all_btn:
                await see_all_btn.click()
                await asyncio.sleep(2)
                await page.wait_for_selector("#aod-offer", timeout=6000)

                sellers = []
                offers = await page.query_selector_all("#aod-offer")
                for offer in offers:
                    entry = {}
                    for s_sel in ["#aod-offer-soldBy a", ".a-col-left .a-size-small a"]:
                        s_el = await offer.query_selector(s_sel)
                        if s_el:
                            entry["seller"] = await safe_text(s_el)
                            break
                    for p_sel in [".aod-price .a-price .a-offscreen", ".a-price .a-offscreen"]:
                        p_el = await offer.query_selector(p_sel)
                        if p_el:
                            entry["price"] = await safe_text(p_el)
                            break
                    ship_el = await offer.query_selector("#aod-offer-shipsFrom, .aod-ships-from-country")
                    if ship_el:
                        ship_text = await safe_text(ship_el)
                        entry["fulfillment"] = "FBA" if "amazon" in ship_text.lower() else "FBM"
                    if entry.get("seller"):
                        sellers.append(entry)

                if sellers:
                    result["Other Sellers"] = " | ".join(
                        f"{s.get('seller','?')} @ {s.get('price','?')} ({s.get('fulfillment','?')})"
                        for s in sellers
                    )

                close_btn = await page.query_selector("#aod-close")
                if close_btn:
                    await close_btn.click()
                    await asyncio.sleep(0.5)
        except:
            pass

    except Exception as e:
        result["Status"] = f"Error: {str(e)[:80]}"

    return result


def _blank_result(asin, status):
    return {
        "ASIN": asin, "URL": f"https://www.amazon.in/dp/{asin}",
        "Title": "", "Rating": "", "No. of Ratings": "", "BSR": "",
        "Buybox Price": "", "Buybox Seller": "", "Buybox Fulfillment": "",
        "Other Sellers": "", "Limited Time Deal": "No",
        "Use By Date": "", "Status": status,
    }


async def scrape_all(asins, progress_callback=None, stop_event=None):
    """
    Queue-based scraper: CONCURRENCY persistent browser contexts, each
    pops ASINs from a shared queue until empty.  Pincode is set ONCE per
    context (not once per ASIN), saving ~12 s of overhead per ASIN.
    """
    asins = [a.strip() for a in asins if a.strip()]
    total = len(asins)
    results = [None] * total
    completed = [0]

    queue: asyncio.Queue = asyncio.Queue()
    for i, asin in enumerate(asins):
        await queue.put((i, asin))

    def is_stopped():
        return stop_event is not None and stop_event.is_set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-zygote",
                "--ipc=host",
            ],
        )

        async def run_slot(slot_id: int):
            # Stagger slot startup: slot 0 starts immediately, others wait
            await asyncio.sleep(slot_id * 3)

            # ── Create ONE context per slot ───────────────────────────────────
            try:
                context = await browser.new_context(
                    user_agent=USER_AGENTS[slot_id % len(USER_AGENTS)],
                    viewport=VIEWPORTS[slot_id % len(VIEWPORTS)],
                    locale="en-IN",
                    timezone_id="Asia/Kolkata",
                    extra_http_headers={
                        "Accept-Language": "en-IN,en;q=0.9",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    },
                )
                page = await context.new_page()
                await configure_page(page)
                await set_pincode(page)   # ← done ONCE per slot, not per ASIN
            except Exception as e:
                # Context failed to start — drain our share of the queue
                while True:
                    try:
                        idx, asin = queue.get_nowait()
                        results[idx] = _blank_result(asin, f"Slot init error: {str(e)[:60]}")
                        completed[0] += 1
                        if progress_callback:
                            progress_callback(completed[0], total, asin)
                    except asyncio.QueueEmpty:
                        break
                return

            # ── Process ASINs from shared queue ───────────────────────────────
            while not is_stopped():
                try:
                    idx, asin = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                # Heartbeat — show current ASIN in UI immediately
                if progress_callback:
                    progress_callback(completed[0], total, asin)

                try:
                    result = await asyncio.wait_for(
                        scrape_asin(page, asin), timeout=60
                    )
                    results[idx] = result

                except asyncio.TimeoutError:
                    results[idx] = _blank_result(asin, "Timeout (60s)")
                    # Reset page state after timeout
                    try:
                        await page.goto("about:blank", timeout=5000)
                    except Exception:
                        pass

                except Exception as e:
                    err = str(e)
                    results[idx] = _blank_result(asin, f"Error: {err[:60]}")

                    # Page/target crashed → recreate page within same context
                    if any(k in err for k in ("Target closed", "Target crashed",
                                              "Session closed", "Connection closed")):
                        try:
                            await page.close()
                        except Exception:
                            pass
                        try:
                            page = await context.new_page()
                            await configure_page(page)
                        except Exception:
                            break   # context itself is dead, abandon slot

                finally:
                    completed[0] += 1
                    if progress_callback:
                        progress_callback(completed[0], total, asin)

                # Short delay between ASINs — looks human, avoids rate-limit
                if not is_stopped():
                    await asyncio.sleep(random.uniform(2, 4))

            try:
                await context.close()
            except Exception:
                pass

        await asyncio.gather(*[run_slot(i) for i in range(CONCURRENCY)])
        await browser.close()

    return [r for r in results if r is not None]

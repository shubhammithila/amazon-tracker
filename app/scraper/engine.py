import asyncio
import logging
import random
from datetime import datetime
from typing import Callable, Optional

import httpx

from app.scraper.http_client import create_client
from app.scraper.parsers import parse_product_page
from app.scraper.stealth import get_random_delay, get_random_headers
from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


class ScrapeState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.running = False
        self.progress = 0
        self.total = 0
        self.current_asin = ""
        self.results: list[dict] = []
        self.round = 1
        self.round_total = settings.scrape_retry_rounds
        self.error_count = 0
        self.last_scraped_at: Optional[str] = None
        self.error: Optional[str] = None
        self._stop_event = asyncio.Event()

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "progress": self.progress,
            "total": self.total,
            "current_asin": self.current_asin,
            "round": self.round,
            "round_total": self.round_total,
            "error_count": self.error_count,
            "last_scraped_at": self.last_scraped_at,
            "error": self.error,
            "result_count": len(self.results),
        }

    def stop(self):
        self._stop_event.set()

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()


scrape_state = ScrapeState()


async def fetch_product_page(client: httpx.AsyncClient, asin: str) -> dict:
    url = f"https://www.amazon.in/dp/{asin}"
    try:
        response = await client.get(url)
        if response.status_code == 503:
            return {"asin": asin, "url": url, "status": "Throttled (503)"}
        if response.status_code == 404:
            return {"asin": asin, "url": url, "status": "Not Found (404)"}
        if response.status_code != 200:
            return {"asin": asin, "url": url, "status": f"HTTP {response.status_code}"}
        text = response.content.decode("utf-8", errors="replace")
        return parse_product_page(text, asin)
    except httpx.TimeoutException:
        return {"asin": asin, "url": url, "status": "Timeout"}
    except httpx.ConnectError:
        return {"asin": asin, "url": url, "status": "Connection Error"}
    except Exception as e:
        return {"asin": asin, "url": url, "status": f"Error: {str(e)[:80]}"}


async def scrape_worker(
    semaphore: asyncio.Semaphore,
    queue: asyncio.Queue,
    client: httpx.AsyncClient,
    state: ScrapeState,
    on_result: Optional[Callable] = None,
):
    while not state.stopped:
        try:
            asin = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        async with semaphore:
            if state.stopped:
                break

            state.current_asin = asin
            delay = get_random_delay(settings.scrape_delay_min, settings.scrape_delay_max)
            await asyncio.sleep(delay)

            result = await fetch_product_page(client, asin)

            for attempt in range(2):
                if result["status"] == "OK" or state.stopped:
                    break
                if result["status"] in ("Throttled (503)", "Timeout", "Connection Error"):
                    backoff = (attempt + 1) * 5 + random.uniform(0, 3)
                    await asyncio.sleep(backoff)
                    client.headers.update(get_random_headers())
                    result = await fetch_product_page(client, asin)

            existing_idx = next(
                (i for i, r in enumerate(state.results) if r["asin"] == asin), None
            )
            if existing_idx is not None:
                state.results[existing_idx] = result
            else:
                state.results.append(result)

            state.progress += 1
            if result["status"] != "OK":
                state.error_count += 1

            if on_result:
                await on_result(result)

            queue.task_done()


async def run_scrape(
    asins: list[str],
    on_result: Optional[Callable] = None,
    on_round_complete: Optional[Callable] = None,
    on_complete: Optional[Callable] = None,
):
    scrape_state.reset()
    state = scrape_state
    state.running = True
    state.total = len(asins)
    state.round_total = settings.scrape_retry_rounds
    logger.info(f"Starting scrape of {len(asins)} ASINs")

    try:
        for round_num in range(1, settings.scrape_retry_rounds + 1):
            if state.stopped:
                break

            state.round = round_num
            state.error_count = 0

            if round_num == 1:
                asins_to_scrape = asins
            else:
                asins_to_scrape = [
                    r["asin"] for r in state.results
                    if r.get("status") != "OK" or not r.get("title")
                ]
                if not asins_to_scrape:
                    break
                state.progress = 0
                state.total = len(asins_to_scrape)

            queue: asyncio.Queue = asyncio.Queue()
            for asin in asins_to_scrape:
                queue.put_nowait(asin)

            semaphore = asyncio.Semaphore(settings.scrape_concurrency)
            client = create_client(timeout=settings.scrape_timeout)

            try:
                workers = [
                    asyncio.create_task(
                        scrape_worker(semaphore, queue, client, state, on_result)
                    )
                    for _ in range(settings.scrape_concurrency)
                ]
                await asyncio.gather(*workers)
            finally:
                await client.aclose()

            if on_round_complete:
                await on_round_complete(round_num, state.error_count)

        state.last_scraped_at = datetime.utcnow().isoformat()

    except Exception as e:
        state.error = str(e)
        logger.exception("Scrape engine error")
    finally:
        state.running = False
        state.current_asin = ""
        if on_complete:
            await on_complete(state.results)

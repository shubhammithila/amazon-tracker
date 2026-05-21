# Amazon Tracker v2 — Project Memory

## Project Overview
Complete rebuild of Amazon product tracker + FBA invoice generator. FastAPI + httpx + lxml replacing Flask + Playwright.

## How to Run
- Double-click `C:\Users\LENOVO\Desktop\Start Amazon Tracker.bat`
- Or manually: `cd` to project dir, `.\venv\Scripts\activate`, `uvicorn app.main:app --reload --port 8000`
- Password: `admin123`
- URL: http://localhost:8000

## Project Location
- Working dir: `C:\Users\LENOVO\Desktop\Claude\Amazon Tracker\.claude\worktrees\stoic-allen-bb3a55`
- Launch file: `.claude/launch.json` (use `preview_start` with name `tracker`)

---

## Architecture
- **Backend**: FastAPI (async) + SQLAlchemy async + SQLite (local) / PostgreSQL (prod)
- **Scraping**: httpx async + lxml XPath (no browser needed)
- **Frontend**: Vanilla JS + Chart.js, WebSocket for live progress
- **Scheduling**: APScheduler for daily auto-scrapes
- **Deployment target**: AWS EC2 t2.micro + RDS PostgreSQL (free tier)

## Key Files
```
app/
├── main.py              # FastAPI app entry point
├── config.py            # Settings (pydantic-settings, .env)
├── database.py          # Async SQLAlchemy engine
├── models.py            # DB models (Products, PriceHistory, BSRHistory, RatingHistory, SellerOffers, Keywords, KeywordRankings, ScrapeJobs, Invoices)
├── scheduler.py         # APScheduler (daily scrape at 06:00, keywords at 07:30)
├── utils.py             # Date parsing helpers
├── routers/
│   ├── auth.py          # Login/logout (session cookie, itsdangerous)
│   ├── scrape.py        # /scrape, /progress, /results, /stop, /fetch-sheet
│   ├── products.py      # /products, /products/{asin}/history, /products/download
│   ├── keywords.py      # /keywords, /keywords/track, /keywords/{id}/rankings
│   ├── ws.py            # WebSocket /ws/progress
│   └── invoice.py       # /invoice/parse-shipment, /invoice/generate-excel, /invoice/generate-pdf, /invoice/save, /invoice/next-number
├── scraper/
│   ├── engine.py        # Async scrape orchestrator (queue, semaphore, retry)
│   ├── http_client.py   # httpx client with stealth headers
│   ├── parsers.py       # lxml XPath extractors (title, price, BSR, rating, seller, fulfillment, deal, use_by)
│   ├── keyword_tracker.py  # Search result rank tracking
│   └── stealth.py       # User agent rotation, delays, headers
└── invoice/
    ├── company_data.py  # F2D Tech GSTINs, supplier info, priority FC addresses, transporters
    ├── hsn_codes.py     # HSN code master (default 1106 @ 5% for all food products)
    ├── parser.py        # Parse Amazon FBA shipment TSV files
    ├── generator.py     # Generate Excel + PDF invoices (reportlab)
    ├── fc_addresses.json    # 93 Amazon FC addresses (from official Excel)
    ├── pricing_data.json    # 410 SKU/ASIN → purchase rate mappings
    └── hsn_master.json      # Verified HSN codes (auto-saved after each invoice)
```

---

## Company Data (F2D Tech Private Limited)

### Supplier Info
- **Name**: F2D TECH PRIVATE LIMITED
- **Address**: C/O Dinesh Prasad Sah, New Babu Para, Near Dadi Shyam Mandir, Dumka, Jharkhand 814101
- **Primary GSTIN** (Jharkhand): 20AAFCF9848M1Z7
- **Phone**: 7870034414

### GSTINs by State
| State | GSTIN |
|-------|-------|
| Assam | 18AAFCF9848M1ZS |
| Bihar | 10AAFCF9848M1Z8 |
| Delhi | 07AAFCF9848M1ZV |
| Gujarat | 24AAFCF9848M1ZZ |
| Haryana | 06AAFCF9848M1ZX |
| Jharkhand | 20AAFCF9848M1Z7 |
| Karnataka | 29AAFCF9848M1ZP |
| Maharashtra | 27AAFCF9848M1ZT |
| Odisha | 21AAFCF9848M1Z5 |
| Punjab | 03AAFCF9848M1Z3 |
| Rajasthan | 08AAFCF9848M1ZT |
| Tamil Nadu | 33AAFCF9848M1Z0 |
| Telangana | 36AAFCF9848M1ZU |
| Uttar Pradesh | 09AAFCF9848M1ZR |
| West Bengal | 19AAFCF9848M1ZQ |

### Priority FC Addresses (most used)
- **ISK3** (Maharashtra): Amazon Seller Services Private Limited, Royal Warehousing and Logistics LLP, Survey Number 45, Hissa No.4A, Village Pise Village, Aamne Post, BHIWANDI, MAHARASHTRA 421302, IN
- **BLR4** (Karnataka): Amazon Seller Services Private Limited, Plot No. 12 P2, Hitech, Defence and Aerospace Park, Devanahalli, BENGALURU, KARNATAKA 562149, IN
- **DED3** (Haryana): ASSPL - Haryana, Block J2, Farukhnagar Logistics Parks, LLP, Village- Farrukhnagar, Tehsil- Farrukhanagar, Gurgaon, HARYANA 122506, IN

### Transporters
- All Cargo Logistics
- VRL Logistics

---

## Invoice System

### Invoice Number Format
- Format: `ST/YY-YY/NNN` (e.g., ST/26-27/028)
- Financial year: April to March
- Last known invoice: #027
- Auto-increments, but user can edit

### HSN Codes
- **Default for all F2D food products**: HSN 1106 (flour/meal/powder of legumes & cereals) at 5% GST
- Verified from existing invoices and GST portal (https://services.gst.gov.in/services/searchhsnsac)
- HSN codes saved to `hsn_master.json` after each invoice finalization — never looked up again
- GST portal requires CAPTCHA so no programmatic lookup possible

### Pricing Source
- Mithila Foods master: `C:\Users\LENOVO\Desktop\bms data\F2D tech pvt ltd\Mithila Foods\Master Pricing Packing.xlsx` (sheet: FULL MASTER, column: Purchase)
- Howrah Foods master: `C:\Users\LENOVO\Desktop\bms data\F2D tech pvt ltd\Howrah Foods\Master Pricing.xlsx` (column: Purchase)
- Both loaded into `pricing_data.json` (410 entries, keyed by FBA SKU and ASIN)

### FC Address Source
- Official Amazon file: `C:\Users\LENOVO\Desktop\FC_address_and_POC_details._CB792038618_.xlsx`
- 93 FC addresses loaded into `fc_addresses.json`
- Priority addresses (BLR4, DED3, ISK3) hardcoded in `company_data.py` with exact text

---

## Scraper Details

### ASIN Validation
- Only accepts ASINs starting with `B0` (10 chars total)
- Rejects FNSKUs (start with X) and other codes

### Scrape Settings (defaults)
- Concurrency: 10 async workers
- Delay: 1.5-3.5s random between requests
- Retry rounds: 3
- Timeout: 15s per request
- Scheduled daily at 06:00

### Data Extracted per ASIN
- Title, Price (₹), Rating, Rating Count, BSR (rank + category), Seller, Fulfillment (FBA/FBM/Easy Ship), Deal status, Use By date

---

## Deployment

### Local (Windows)
- Python 3.11+ with venv
- SQLite database (`tracker.db`)
- `Start Amazon Tracker.bat` on desktop

### Production (AWS Free Tier)
- EC2 t2.micro + RDS PostgreSQL
- Caddy for HTTPS reverse proxy
- systemd service for auto-restart
- Setup script: `deploy/setup-ec2.sh`
- For PostgreSQL: add `asyncpg` to requirements, set `DATABASE_URL` env var

---

## Dependencies
```
fastapi, uvicorn[standard], sqlalchemy[asyncio], aiosqlite, alembic,
pydantic-settings, httpx, lxml, pandas, openpyxl, apscheduler,
python-multipart, itsdangerous, jinja2, aiofiles, reportlab
```

## Git
- Branch: `claude/stoic-allen-bb3a55`
- Remote: https://github.com/shubhammithila/amazon-tracker.git
- Auth issue: `gh` CLI not installed, need PAT or `gh auth login` to push

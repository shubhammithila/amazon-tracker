# Parser-library spike: measured, not chosen on taste

Run against **three real Amazon.in product pages** (2.2–2.3 MB each, fetched live and saved to
`spike/`) with the Python parser's output as ground truth (`spike/python_expected.json`).

The question mattered because `app/scraper/parsers.py` is 394 lines of XPath **tuned against
live pages**, and its value is in the tuning rather than the structure: `extract_price` tries six
selectors in order, and `extract_deal` exists because a hardcoded phrase list silently reported
"No" for every product during the Freedom Sale. Porting that is re-verification, not translation.

## Result

| Library | Selectors | Correct | Speed | Memory |
|---|---|---|---|---|
| **cheerio** | rewritten to CSS | **15/15 pages** | **274 ms/page** | +39 MB/page |
| `xpath` + `@xmldom/xmldom` | **identical to lxml** | **0/15** | — | +12 MB |
| `jsdom` | identical to lxml | 15/15 | **2,240 ms/page** | **+511 MB** |

**cheerio wins, and the two rejections are decisive rather than marginal.**

- **`@xmldom/xmldom` parses XML strictly and Amazon's HTML is not XML.** It failed on the first
  page with `Opening and ending tag mismatch: "div" != "br"` — an unclosed `<br>`, which every
  real page has. 0 of 15. The attraction was keeping the XPath expressions byte-identical to
  lxml's, which would have made the port a copy; that option does not exist.
- **`jsdom` is correct but unaffordable.** 2,240 ms/page against cheerio's 274 (**8×**) and
  +511 MB RSS. The production box has 951 MB and has already OOM-killed a `pip install`; the
  scrape engine runs 10 concurrent workers, so per-page cost is multiplied. It builds a full
  DOM with CSS and script support to answer an XPath query.

So the selectors get rewritten to CSS. `contains(@class,"x")` becomes `[class*="x"]`, and the
one union — `//table[contains(@id,"productDetails")]//tr | //div[@id="…"]//li` — becomes a
comma list, verified to return the same **11** rows.

## The port must reproduce two behaviours that a naive rewrite loses

Both were found by diffing against the Python output rather than by writing tests that agree
with themselves. This is the whole argument for the differential test being the acceptance bar.

### 1. `.text()` on a multi-node cheerio selection CONCATENATES

lxml's `tree.xpath(...)` returns a list and the Python code takes `result[0]`. cheerio's
`$(sel).text()` joins the text of **every** match. Measured on `B0D817HX57`, whose last-resort
selector matches **21** price nodes:

```
$(sel).text()         -> "294.494.597.35294.494.47.597.175.181.450"
$(sel).first().text() -> "294."
frac.text()           -> "0000000000000000000000000000000000000000"
```

A price of `₹294..00` is obviously wrong. **`₹294.00` would not have been** — it is a real price
on that page, just not the one being asked for, and on a page with fewer matches the
concatenation produces a plausible number. Every ported selector therefore takes `.first()`, and
that is a rule for the whole file rather than a fix for one function.

### 2. `detect_unavailable` clears the price to NULL on purpose

The remaining disagreement was `python=null` against `node=₹294..00`, and **Python is right**.
`parse_product_page` checks `detect_unavailable` and, when the listing exists but cannot be
bought, returns title/rating/BSR while explicitly nulling `price`, `seller`, `fulfillment` and
setting `deal` to "No" — so stale data cannot persist as though it were current. Measured on
that page: `#corePrice_feature_div` and `priceToPay` are both **absent**, which is why the
six-selector chain falls through to the ad-carousel prices of *other* products.

A rewrite that ports the extractors without the three guards
(`detect_captcha`, `detect_dog_page`, `detect_unavailable`) and the no-title check would write a
neighbouring product's price against an unavailable ASIN, and the row would look entirely
normal. The guards are the load-bearing part.

## What this fixes about the plan

The port order is now: **guards first, extractors second.** The original file is structured the
other way round (extractors at the top, `parse_product_page` last), and reading it top-down is
what led to a spike that got 11 of 12 fields right and the important one wrong.

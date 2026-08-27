"""Portfolio Review — which products earn their place, and which to churn.

Four modules, the same split the Orders feature uses:

* ``economics`` — the only caller of Amazon's Data Kiosk. Fetches the Seller Central
  Economics figures: sales, refunds, every fee, ad spend and net proceeds per ASIN.
* ``logic`` — pure rules. Joins economics to the live product catalogue and to our own
  review scraper, rolls child ASINs up into parent products, and decides a verdict.
* ``repository`` — the only reader and writer of the stored snapshot and of the owner's
  decisions.
* ``refresh`` — the background job, because a Data Kiosk query takes one to two minutes
  and must never be awaited inside a request.

**This tab replaced a CSV upload.** The old Portfolio tab asked the owner to download a
Business Report from Seller Central and upload it, then wrote the result to a JSON blob at
repo root. The analysis was therefore stale the moment it was saved and could not be
repeated without a human. Everything here comes from the API instead.
"""

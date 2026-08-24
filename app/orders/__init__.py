"""Amazon Easy Ship orders: the local cache, the refresh job and the picking sheet.

Mirrors app/shipment/'s split — logic.py is pure, spapi_orders.py is the only caller of
Amazon, repository.py is the only reader/writer of rows. That separation is what makes
the phase-B shipping actions additive rather than a rewrite.
"""

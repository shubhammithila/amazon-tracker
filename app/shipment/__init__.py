"""Shipment planning: rounding, ordering, packing carry-over, and documents.

`logic.py` holds pure functions with no FastAPI or database imports. Everything
that decides a number — how a plan quantity is rounded, what order rows appear
in, whether a day's packing is too small to ship — lives there and is called
from exactly one place, so the dashboard and every download always agree.
"""

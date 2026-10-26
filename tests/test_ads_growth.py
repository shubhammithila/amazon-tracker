"""Ads → Keyword growth: relevant keywords and competitor targets, added at a bid the owner chooses.

Spec: `docs/superpowers/specs/2026-10-26-ads-keyword-growth-design.md`.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ─── The tabs ────────────────────────────────────────────────────────────────


async def test_the_three_ads_tabs_read_in_order_on_every_ads_page(auth_client):
    for path, on in (("/ads-page", "Optimisation"), ("/ads-page/expansion", "SKU coverage"),
                     ("/ads-page/growth", "Keyword growth")):
        response = await auth_client.get(path)
        assert response.status_code == 200, path
        html = response.text
        positions = [html.index(f">{label}</a>") for label in ("Optimisation", "SKU coverage", "Keyword growth")]
        assert positions == sorted(positions), path
        assert f'on" href="{path}">{on}</a>' in html, path

"""product_prices table, seeded from pricing_data.json

A home for purchase rates that a deploy cannot revert.

``app/invoice/pricing_data.json`` holds 410 rates and is TRACKED IN GIT. Editing prices in
the app would mean writing to it at runtime, which is exactly the problem
``hsn_master.json`` already causes: ``deploy/update-ec2.sh`` has to stash and restore that
file by hand, because otherwise a checkout silently reverts data the owner typed. Rows in a
table are not touched by a checkout.

**The seed reads the JSON at migration time, deliberately breaking the usual rule that
schema history should not read application files.** The alternative is worse. The 410 rates
already exist and are correct; regenerating them by hand is a day of work and a chance to
introduce errors, and an empty table would make every product read "not priced" on the new
screen. The file is read defensively — a missing or malformed file leaves the table empty
rather than failing the deploy, and the runtime lookup falls back to the JSON anyway.

Keyed by ASIN, while the JSON is keyed by BOTH sku and asin (hence 410 entries for ~205
products). The ASIN is the stable identifier: merchant SKUs are blank on 108 sheet rows,
arrive from the uploaded CSV, and are edited by hand on the plan.

Revision ID: 9e4b1c7a2f56
Revises: 7c1a4e9b2d38
Create Date: 2026-08-16

"""
import json
import logging
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger("alembic.runtime.migration")

# revision identifiers, used by Alembic.
revision: str = "9e4b1c7a2f56"
down_revision: Union[str, None] = "7c1a4e9b2d38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _seed_rows() -> list[dict]:
    """{asin: rate} from the JSON, plus names from product_families.json where known.

    Both files are read defensively. A migration that dies because an application data
    file moved is a failed deploy for no good reason, and every value here is recoverable:
    the runtime lookup still falls back to the JSON when a row is absent.
    """
    base = Path(__file__).resolve().parent.parent.parent / "app" / "invoice"

    def load(name: str) -> dict:
        path = base / name
        if not path.exists():
            logger.warning("product_prices seed: %s not found, skipping", name)
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle) or {}
        except Exception as exc:                      # noqa: BLE001 - see docstring
            logger.warning("product_prices seed: could not read %s (%s)", name, exc)
            return {}

    pricing = load("pricing_data.json")
    families = load("product_families.json")

    rows = []
    for key, value in pricing.items():
        asin = str(key).strip().upper()
        # Only the ASIN-shaped keys. The SKU keys carry the same rates — the JSON stores
        # each price twice — so taking both would need a merge and gain nothing.
        if not (len(asin) == 10 and asin.startswith("B0")):
            continue
        try:
            rate = float(value)
        except (TypeError, ValueError):
            continue
        if rate <= 0:
            # 0 is not a price. Left out so the row reads "not priced yet" rather than
            # sending Amazon a declared value it rejects.
            continue

        info = families.get(asin) or families.get(key) or {}
        rows.append({
            "asin": asin,
            "item": (info.get("parent_product") or "").strip() or None,
            "weight": info.get("weight"),
            "brand": "MF" if "mithila" in str(info.get("brand", "")).lower() else (
                "HF" if info.get("brand") else None
            ),
            "purchase_rate": rate,
            "hsn_code": "1106",
            "gst_rate": 5,
        })
    return rows


def upgrade() -> None:
    op.create_table(
        "product_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asin", sa.String(length=20), nullable=False),
        sa.Column("item", sa.Text(), nullable=True),
        sa.Column("fba_sku", sa.String(length=80), nullable=True),
        sa.Column("weight", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("brand", sa.String(length=10), nullable=True),
        sa.Column("purchase_rate", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("hsn_code", sa.String(length=10), nullable=True),
        sa.Column("gst_rate", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("product_prices", schema=None) as batch_op:
        batch_op.create_index(
            "idx_product_prices_asin", ["asin"], unique=True
        )

    rows = _seed_rows()
    if rows:
        # Bulk insert against a lightweight table definition rather than importing the
        # model: schema history must not depend on the current shape of app/models.py,
        # which will keep changing after this revision has run everywhere.
        table = sa.table(
            "product_prices",
            sa.column("asin", sa.String),
            sa.column("item", sa.Text),
            sa.column("weight", sa.Numeric),
            sa.column("brand", sa.String),
            sa.column("purchase_rate", sa.Numeric),
            sa.column("hsn_code", sa.String),
            sa.column("gst_rate", sa.Numeric),
        )
        op.bulk_insert(table, rows)
        logger.info("product_prices: seeded %d rates from pricing_data.json", len(rows))


def downgrade() -> None:
    """Drops the table, losing any price typed on the Products tab.

    Stated rather than glossed: rates that came from pricing_data.json survive in that
    file, but anything the owner typed in the app exists only here.
    """
    with op.batch_alter_table("product_prices", schema=None) as batch_op:
        batch_op.drop_index("idx_product_prices_asin")
    op.drop_table("product_prices")

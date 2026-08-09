from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, Numeric, ForeignKey, Index
)
from sqlalchemy.orm import relationship
from app.database import Base


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    asin = Column(String(10), unique=True, nullable=False, index=True)
    title = Column(Text)
    category = Column(Text)
    use_by = Column(String(50))
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_scraped = Column(DateTime)
    is_active = Column(Boolean, default=True)

    prices = relationship("PriceHistory", back_populates="product", lazy="selectin")
    bsr_entries = relationship("BSRHistory", back_populates="product", lazy="selectin")
    ratings = relationship("RatingHistory", back_populates="product", lazy="selectin")
    seller_offers = relationship("SellerOffer", back_populates="product", lazy="selectin")
    keyword_rankings = relationship("KeywordRanking", back_populates="product", lazy="selectin")


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (
        Index("idx_price_history_product_date", "product_id", "scraped_at"),
    )

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    price = Column(Numeric(10, 2))
    seller = Column(String(255))
    fulfillment = Column(String(20))
    is_deal = Column(Boolean, default=False)
    scraped_at = Column(DateTime, default=datetime.utcnow)

    product = relationship("Product", back_populates="prices")


class BSRHistory(Base):
    __tablename__ = "bsr_history"
    __table_args__ = (
        Index("idx_bsr_history_product_date", "product_id", "scraped_at"),
    )

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    bsr_rank = Column(Integer)
    bsr_category = Column(Text)
    scraped_at = Column(DateTime, default=datetime.utcnow)

    product = relationship("Product", back_populates="bsr_entries")


class RatingHistory(Base):
    __tablename__ = "rating_history"
    __table_args__ = (
        Index("idx_rating_history_product_date", "product_id", "scraped_at"),
    )

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    rating = Column(Numeric(2, 1))
    rating_count = Column(Integer)
    scraped_at = Column(DateTime, default=datetime.utcnow)

    product = relationship("Product", back_populates="ratings")


class SellerOffer(Base):
    __tablename__ = "seller_offers"
    __table_args__ = (
        Index("idx_seller_offers_product", "product_id", "scraped_at"),
    )

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    seller_name = Column(String(255))
    price = Column(Numeric(10, 2))
    fulfillment = Column(String(20))
    is_buybox = Column(Boolean, default=False)
    condition = Column(String(50), default="New")
    scraped_at = Column(DateTime, default=datetime.utcnow)

    product = relationship("Product", back_populates="seller_offers")


class Keyword(Base):
    __tablename__ = "keywords"

    id = Column(Integer, primary_key=True)
    keyword = Column(Text, nullable=False)
    marketplace = Column(String(10), default="in")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    rankings = relationship("KeywordRanking", back_populates="keyword", lazy="selectin")


class KeywordRanking(Base):
    __tablename__ = "keyword_rankings"
    __table_args__ = (
        Index("idx_keyword_rankings_lookup", "keyword_id", "product_id", "scraped_at"),
    )

    id = Column(Integer, primary_key=True)
    keyword_id = Column(Integer, ForeignKey("keywords.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    rank_position = Column(Integer)
    page_number = Column(Integer)
    is_sponsored = Column(Boolean, default=False)
    scraped_at = Column(DateTime, default=datetime.utcnow)

    keyword = relationship("Keyword", back_populates="rankings")
    product = relationship("Product", back_populates="keyword_rankings")


class ScrapeJob(Base):
    __tablename__ = "scrape_jobs"

    id = Column(Integer, primary_key=True)
    job_type = Column(String(20))
    total_items = Column(Integer)
    completed = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    status = Column(String(20), default="pending")
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    error_log = Column(Text)


class ChurnReport(Base):
    __tablename__ = "churn_reports"

    id = Column(Integer, primary_key=True)
    report_date = Column(DateTime, default=datetime.utcnow)
    period_label = Column(String(50))   # e.g. "May 2026"
    total_asins = Column(Integer)
    keep_count = Column(Integer, default=0)
    monitor_count = Column(Integer, default=0)
    churn_count = Column(Integer, default=0)
    no_data_count = Column(Integer, default=0)

    scores = relationship("ChurnScore", back_populates="report", lazy="selectin")


class ChurnScore(Base):
    __tablename__ = "churn_scores"
    __table_args__ = (
        Index("idx_churn_scores_report_asin", "report_id", "asin"),
    )

    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("churn_reports.id"), nullable=False)
    asin = Column(String(10), nullable=False, index=True)
    title = Column(Text)
    score = Column(Integer)                   # 0–100
    status = Column(String(10))               # keep / monitor / churn / no_data
    units_ordered = Column(Integer)
    revenue = Column(Numeric(12, 2))
    conversion_rate = Column(Numeric(5, 2))
    sessions = Column(Integer)
    buy_box_pct = Column(Numeric(5, 2))
    rating = Column(Numeric(3, 1))
    review_count = Column(Integer)
    bsr_current = Column(Integer)
    bsr_trend = Column(String(20))            # improving / declining / stable / unknown
    listing_age_days = Column(Integer)
    reason = Column(Text)                     # human-readable explanation

    report = relationship("ChurnReport", back_populates="scores")


class ShipmentPlan(Base):
    """One weekly shipment plan generated from a sales + stock CSV upload.

    Replaces the single shipment_plan.json blob at repo root. Two roles now write
    concurrently (the owner edits plan quantities, ops records packing), and the
    old whole-file overwrite let either clobber the other.
    """
    __tablename__ = "shipment_plans"

    id = Column(Integer, primary_key=True)
    label = Column(String(100))
    multiplier = Column(Numeric(4, 1), default=5.0)
    # draft / active / closed.
    #
    # `draft` is where a generated plan starts: the owner deletes rows, fixes
    # quantities and fills missing SKUs while the WAREHOUSE STILL SEES THE OLD
    # ACTIVE PLAN. Only `finalise` promotes it. That gap matters — without it the
    # packer could start boxing rows the owner was about to remove.
    #
    # repository.get_active_plan() selects `active` only, which is what keeps all
    # the pre-existing packing endpoints draft-safe by omission rather than by
    # eleven separate edits.
    status = Column(String(20), default="active")
    # Carry-over thresholds: a day is held only when cartons AND units are both
    # below these (see app/shipment/logic.is_held).
    min_cartons = Column(Integer, default=25)
    min_units = Column(Integer, default=500)
    created_at = Column(DateTime, default=datetime.utcnow)

    items = relationship(
        "ShipmentPlanItem", back_populates="plan", lazy="selectin",
        cascade="all, delete-orphan",
    )
    days = relationship(
        "ShipmentPackingDay", back_populates="plan", lazy="selectin",
        cascade="all, delete-orphan",
    )


class ShipmentPlanItem(Base):
    """One SKU line in a plan. Only the owner writes these."""
    __tablename__ = "shipment_plan_items"
    __table_args__ = (
        Index("idx_shipment_plan_items_plan_asin", "plan_id", "asin"),
    )

    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("shipment_plans.id"), nullable=False)
    asin = Column(String(10), nullable=False)
    fba_sku = Column(String(100))
    brand = Column(String(4))
    item = Column(Text)
    # Casefolded parent_product, so SQL ORDER BY matches app.shipment.logic.sort_key.
    # Doubles as the join key into product_categories.
    sort_product = Column(String(120))
    weight = Column(Numeric(6, 3))

    # Brand as a sortable rank: 0 Mithila Foods, 1 Howrah Foods, 2 unknown.
    # Needed because the stored codes are 'MF' and 'HF' and those cannot order
    # alphabetically — H sorts before M. Brand never changes for an ASIN, so
    # persisting the rank carries no staleness risk. See logic.brand_rank_for.
    brand_rank = Column(Integer, default=2, nullable=False)

    # Set when the owner removes the row from the plan. A TIMESTAMP rather than a
    # boolean for two reasons: `WHERE excluded_at IS NULL` treats every
    # pre-migration row as included with no backfill, and a nullable Boolean
    # invites `== False`, which silently drops legacy NULL rows. It also records
    # when, which the "show excluded" toggle displays.
    #
    # Reversible on purpose — an accidental multi-row exclude is one click back.
    excluded_at = Column(DateTime)

    # Snapshot of the CSV upload. Never rewritten after /generate, so the plan
    # always shows the numbers it was actually built from.
    sales_7d = Column(Integer, default=0)
    projection = Column(Integer, default=0)
    fba_stock = Column(Integer, default=0)
    deficit = Column(Integer, default=0)

    # Owner-editable. `shipment_plan` is stored already rounded to the nearest
    # 10; a manual override is kept verbatim. `available` is finished stock on the
    # warehouse shelf and drives the "To make" figure (logic.still_to_source).
    shipment_plan = Column(Integer, default=0)
    available = Column(Integer, default=0)
    s = Column(Boolean, default=False)
    m = Column(Boolean, default=False)
    b = Column(Boolean, default=False)

    plan = relationship("ShipmentPlan", back_populates="items")


class ProductCategory(Base):
    """Sort priority for a parent product: P1 Sattu … P6 Rest.

    A table of its own, keyed by product rather than by plan item, for one
    reason: the owner classifies a PRODUCT once and expects it to hold. There are
    74 distinct products behind 205 ASINs, so per-item storage would mean
    re-making the same 74 decisions on every weekly upload.

    Deliberately NOT denormalised onto the item row and NOT baked into a stored
    composite sort key. ``repository.load_plan_items`` joins this table and orders
    on the joined column, so re-classifying a product needs no row rewrite and
    cannot leave an existing plan silently mis-sorted against a stale key.

    Rows are seeded from ``logic.category_for`` keyword defaults at generate time
    and then overridden by hand; ``source`` records which, so the UI can show
    what was guessed versus what was chosen.
    """
    __tablename__ = "product_categories"
    __table_args__ = (
        # UNIQUE: one priority per product, and the upsert target.
        Index("idx_product_categories_key", "product_key", unique=True),
    )

    id = Column(Integer, primary_key=True)
    # Casefolded parent product name — matches ShipmentPlanItem.sort_product,
    # which is the join key.
    product_key = Column(String(120), nullable=False)
    # The name as it reads on screen, for the category editor.
    product_label = Column(Text)
    priority = Column(Integer, default=6, nullable=False)
    source = Column(String(10), default="keyword")  # keyword / manual
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ShipmentPackingDay(Base):
    """One calendar day of packing against a plan.

    status flow: open -> submitted (ops finished the day) -> verified (owner
    approved) -> shipped (invoice attached). `held` means the day was too small
    to ship alone and is waiting to be combined with later packing.
    """
    __tablename__ = "shipment_packing_days"
    __table_args__ = (
        # Unique so a repeated submit updates the day instead of duplicating it.
        Index("idx_packing_days_plan_date", "plan_id", "pack_date", unique=True),
    )

    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("shipment_plans.id"), nullable=False)
    # 'YYYY-MM-DD'. A string, matching Invoice.date, because the UI posts a
    # plain <input type="date"> and the business runs in IST while the server
    # stores UTC — an explicit date avoids a late-evening entry landing on the
    # wrong day.
    pack_date = Column(String(10), nullable=False)
    status = Column(String(12), default="open")
    hold_reason = Column(Text)
    # Denormalised from the entry rows, recomputed on every write so the day list
    # and the hold check never have to load every entry.
    total_units = Column(Integer, default=0)

    # **Cartons are a property of the DAY, not of a SKU.** Entered directly here,
    # not summed from anything.
    #
    # The owner's words: "carton is not item wise. it is random. like 500 units
    # packed today in 20 cartons." A carton on this floor is filled with whatever
    # is being packed at the time, so a mixed box belongs to several ASINs and to
    # none of them. It was previously recorded per (day, ASIN) and summed here,
    # which asked the packer a question he could not answer — so he either guessed
    # or left it blank, and the number that prefills a GST invoice's Boxes field
    # was a guess.
    total_cartons = Column(Integer, default=0)
    submitted_by = Column(String(20))  # 'ops' / 'admin'
    submitted_at = Column(DateTime)
    verified_at = Column(DateTime)
    invoice_id = Column(Integer, ForeignKey("invoices.id"))

    plan = relationship("ShipmentPlan", back_populates="days")
    entries = relationship(
        "ShipmentPackingEntry", back_populates="day", lazy="selectin",
        cascade="all, delete-orphan",
    )


class ShipmentPackingEntry(Base):
    """Units packed for one SKU on one day. Only ops writes these.

    Units only. Cartons live on the DAY — see
    ``ShipmentPackingDay.total_cartons``. A carton here holds whatever was being
    packed when it was filled, so it cannot be attributed to a single ASIN, and
    asking for a per-SKU count produced guesses.
    """
    __tablename__ = "shipment_packing_entries"
    __table_args__ = (
        # Unique so a double-save upserts rather than double-counting the units.
        Index("idx_packing_entries_day_asin", "day_id", "asin", unique=True),
    )

    id = Column(Integer, primary_key=True)
    day_id = Column(Integer, ForeignKey("shipment_packing_days.id"), nullable=False)
    asin = Column(String(10), nullable=False)
    units = Column(Integer, default=0)
    note = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    day = relationship("ShipmentPackingDay", back_populates="entries")


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True)
    invoice_no = Column(String(30), unique=True, nullable=False)
    invoice_number = Column(Integer, nullable=False)  # Sequential number (27, 28, 29...)
    shipment_id = Column(String(50))
    date = Column(String(20))
    supplier_gstin = Column(String(20))
    recipient_gstin = Column(String(20))
    recipient_state = Column(String(50))
    fc_code = Column(String(10))
    transporter = Column(String(100))
    total_qty = Column(Integer)
    total_taxable = Column(Numeric(12, 2))
    total_igst = Column(Numeric(12, 2))
    total_amount = Column(Numeric(12, 2))
    invoice_data = Column(Text)  # Full JSON of the invoice
    created_at = Column(DateTime, default=datetime.utcnow)

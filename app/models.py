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
    status = Column(String(20), default="active")  # active / closed
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
    sort_product = Column(String(120))
    weight = Column(Numeric(6, 3))

    # Snapshot of the CSV upload. Never rewritten after /generate, so the plan
    # always shows the numbers it was actually built from.
    sales_7d = Column(Integer, default=0)
    projection = Column(Integer, default=0)
    fba_stock = Column(Integer, default=0)
    deficit = Column(Integer, default=0)

    # Owner-editable. Stored already rounded to the nearest 10; a manual
    # override is kept verbatim.
    shipment_plan = Column(Integer, default=0)
    available = Column(Integer, default=0)
    s = Column(Boolean, default=False)
    m = Column(Boolean, default=False)
    b = Column(Boolean, default=False)

    plan = relationship("ShipmentPlan", back_populates="items")


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
    # Denormalised totals, recomputed on every write so the day list and the
    # hold check never have to load every entry.
    total_units = Column(Integer, default=0)
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
    """Units and cartons packed for one SKU on one day. Only ops writes these."""
    __tablename__ = "shipment_packing_entries"
    __table_args__ = (
        # Unique so a double-save upserts rather than double-counting the units.
        Index("idx_packing_entries_day_asin", "day_id", "asin", unique=True),
    )

    id = Column(Integer, primary_key=True)
    day_id = Column(Integer, ForeignKey("shipment_packing_days.id"), nullable=False)
    asin = Column(String(10), nullable=False)
    units = Column(Integer, default=0)
    cartons = Column(Integer, default=0)
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

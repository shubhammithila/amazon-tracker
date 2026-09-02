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
    # When the owner retired this plan. Distinct from a plan merely being superseded:
    # `status` says WHAT it is, this says WHEN it stopped, which is what the history
    # list sorts and labels by. Nullable, so no backfill is needed for plans closed
    # before this column existed.
    closed_at = Column(DateTime)

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


class ProductPrice(Base):
    """Purchase rate, HSN code and GST rate for one ASIN. Edited on the Products tab.

    **A table rather than the JSON file it replaces.** `pricing_data.json` holds 410 rates
    and is TRACKED IN GIT, so writing to it at runtime creates exactly the problem
    `hsn_master.json` already causes on every deploy: the file has to be stashed and
    restored by hand or a checkout silently reverts real data. Rows here are safe from a
    deploy.

    Keyed by ASIN, not by merchant SKU. The SKU is the thing that changes — it is blank on
    108 sheet rows, arrives from the uploaded CSV, and gets edited by hand on the plan —
    while the ASIN identifies the product for its whole life. `pricing_data.json` is keyed
    by both, which is why it has 410 entries for ~205 products.

    A missing row means "not priced yet", which is a state the app must handle rather than
    treat as zero: Amazon rejects an inbound shipment whose declared value is 0, and it
    does so with "We encountered an internal error" — a message that looks like a fault on
    their side. So price is nullable and the shipment flow refuses those products by name.
    """
    __tablename__ = "product_prices"
    __table_args__ = (
        # UNIQUE: one price per ASIN, and the upsert target.
        Index("idx_product_prices_asin", "asin", unique=True),
    )

    id = Column(Integer, primary_key=True)
    asin = Column(String(20), nullable=False)
    # Denormalised for the Products screen only. The catalogue is the source of truth for
    # names and pack sizes; these are a snapshot so the table still reads sensibly when
    # the MRP sheet is unreachable.
    item = Column(Text)
    fba_sku = Column(String(80))
    weight = Column(Numeric(10, 3))
    brand = Column(String(10))
    # Nullable on purpose — see the class docstring. NULL is "not priced yet", 0 would be
    # a declared value Amazon rejects.
    purchase_rate = Column(Numeric(12, 2))
    # 1106 at 5% for every F2D product today, but editable: a non-food line would be
    # classified differently, and a wrong HSN on a GST document is worse than a blank one.
    hsn_code = Column(String(10), default="1106")
    gst_rate = Column(Numeric(5, 2), default=5)
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

    # ── The Amazon shipment these boxes went into ────────────────────────────
    #
    # Recorded per DAY, matching invoice_id above, because a shipment covers the same
    # chosen set of days an invoice does — that is what makes the two agree.
    #
    # Written whether the shipment was created through SP-API or by hand in Send to
    # Amazon: the value is the same fact either way, and storing it only for the
    # automated path would leave the manual path looking un-shipped exactly as it
    # previously looked un-invoiced.
    #
    # `shipment_confirmation_id` is the FBA15… string that goes on the GST invoice.
    # `inbound_plan_id` and `amazon_shipment_id` are Amazon's internal handles, kept
    # because getShipment and getLabels need them and they are not derivable from the
    # confirmation id.
    inbound_plan_id = Column(String(60))
    amazon_shipment_id = Column(String(60))
    shipment_confirmation_id = Column(String(30))
    # From Amazon's own answer, not from the FC the owner picked. The destination
    # decides which of the 15 GSTINs applies, so the two must not be conflated: his
    # pick is a request, this is what actually happened.
    destination_warehouse_id = Column(String(10))
    destination_state = Column(String(50))
    # The plan this day was packed against BEFORE it was carried forward, or NULL if
    # it has always belonged to its current plan.
    #
    # A plain Integer, deliberately NOT a ForeignKey: the source plan can be deleted
    # (DELETE /shipment/plan/{id} cascades), and a FK would either block that delete
    # or null the lineage out. This column's only job is to explain, on screen and in
    # a reconciliation, why a plan holds units for a date it never opened — so an id
    # that no longer resolves is still better than no id at all.
    carried_from_plan_id = Column(Integer)

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


class User(Base):
    """A named login with per-area permissions.

    Replaces the two shared passwords (``APP_PASSWORD`` / ``OPS_PASSWORD``) as the way
    people sign in. Those still work — see ``app/routers/auth.py`` — because removing
    them in the same change that adds this table would mean a deploy where nobody can
    log in if anything about the table is wrong.

    ``permissions`` is a comma-separated list of area keys, read through
    ``app.permissions.parse``. Not a join table: six possible values per user, and a
    join would be extra queries on every request for a set that fits in a column. Not
    JSON either — neither is indexable, and a plain string is the one a human can read
    in a sqlite3 shell when something has gone wrong.

    ``is_admin`` is a separate flag rather than an area, because "can change what other
    people see" is a different kind of power from "can see the invoice tab". Keeping
    them apart is what stops a user granting themselves the rest.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    # Stored lowercased (see credentials.normalise_username) so `Ravi` and `ravi`
    # cannot become two accounts with different permissions.
    username = Column(String(32), unique=True, nullable=False, index=True)
    full_name = Column(String(120))
    password_hash = Column(String(255), nullable=False)
    permissions = Column(String(255), default="")
    is_admin = Column(Boolean, default=False, nullable=False)
    # Disabled rather than deleted: a departed packer's name still appears on the
    # packing days he submitted, and deleting the row would orphan that history.
    is_active = Column(Boolean, default=True, nullable=False)
    # Who created whom, for the case where two people share the owner login and
    # neither remembers adding an account.
    created_by = Column(String(32))
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime)
    # Set when the owner generates or resets a password, cleared on first successful
    # login. Lets the panel show "has not signed in yet" honestly.
    must_change_password = Column(Boolean, default=False, nullable=False)


class UserLoginEvent(Base):
    """One login attempt — successful or not. **The login history this app never had.**

    `User.last_login_at` is a single timestamp, overwritten on every success, so it can answer
    "when did they last sign in" and nothing else — not "how often," not "who tried and failed,"
    not "which of the three login paths did this go through." No audit table existed anywhere
    in this codebase before this one.

    **Every attempt is recorded, success or failure** — a log that only shows successes cannot
    answer "is someone trying my password," which is the more common reason to open this page.

    `username` is the string TYPED, not resolved — a failed attempt against a username that does
    not exist is still worth recording, and there is no user row to attach it to in that case.
    `user_id` is set only when the attempt succeeded against a real named account, and stays NULL
    for every shared-password login (no user row exists) and every failed one.

    `via` distinguishes the three paths `POST /login` can take, so a shared-password sign-in does
    not read as though it were a named one on the same log.
    """
    __tablename__ = "user_login_events"
    __table_args__ = (
        Index("idx_user_login_events_created", "created_at"),
        Index("idx_user_login_events_user", "user_id"),
    )

    id = Column(Integer, primary_key=True)
    username = Column(String(32), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    success = Column(Boolean, nullable=False)
    #: "named" | "app_password" | "ops_password".
    via = Column(String(16), nullable=False)
    #: From X-Forwarded-For (Caddy sits in front of uvicorn in production) with a fallback to
    #: the raw client address for local/dev runs with no proxy in front.
    ip_address = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)


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


class AmazonOrder(Base):
    """One Amazon order, as Amazon last reported it.

    **A cache of Amazon's data, not a record of our own.** Nothing in this app edits
    these rows: if a value looks wrong the fix is a refresh, which is why there is no
    editing anywhere in the Orders feature and no local "packed" tick. A second source
    of truth about whether an order shipped is the class of bug the shipment feature's
    write-separation design exists to avoid.

    Stored rather than fetched per request because `getOrders` is rate-limited to
    **0.045 requests/second** — one call every 22 seconds, measured from the live
    account. A page that called it would hang, and two people opening the tab would 429.
    """
    __tablename__ = "amazon_orders"

    id = Column(Integer, primary_key=True)
    #: Amazon's own id ("403-7588486-5589960"). UNIQUE so a re-refresh UPDATES rather
    #: than duplicating — the same reasoning as the (plan_id, pack_date) index on
    #: packing days, where a repeated save from a warehouse phone must not double-count.
    amazon_order_id = Column(String(20), unique=True, nullable=False, index=True)

    # ── Timestamps: stored UTC, rendered IST. See app.orders.logic.to_ist. ──
    #
    # The `_utc` suffix is load-bearing. Every real LatestShipDate is 18:29Z = 23:59 IST,
    # so a reader who renders these directly shows every deadline 5.5 hours early.
    purchase_date_utc = Column(DateTime)
    latest_ship_date_utc = Column(DateTime)

    status = Column(String(20))              # Unshipped / Shipped / Canceled …
    easyship_status = Column(String(30))     # PendingSchedule / PickedUp / Delivered …
    #: Contains "EZ" for Easy Ship. This is the field that identifies the channel —
    #: FulfillmentChannel is MFN for both Easy Ship and plain self-ship.
    ship_service_level = Column(String(60))

    order_total = Column(Numeric(12, 2))
    currency = Column(String(5))
    items_ordered = Column(Integer, default=0)
    items_shipped = Column(Integer, default=0)
    is_prime = Column(Boolean, default=False)
    #: True when ship_service_level mentions COD. Read off the service level rather than
    #: PaymentMethod, which reads "Other" on real COD orders.
    is_cod = Column(Boolean, default=False)

    # Destination, coarse. City/state/postcode is all Amazon gives without the PII role,
    # and it is all a picking sheet needs — no buyer name, no street address.
    city = Column(String(60))
    state = Column(String(60))
    postal_code = Column(String(12))

    #: When this app first saw the order, so "new since I last looked" is answerable
    #: separately from "Amazon changed something".
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    last_refreshed_at = Column(DateTime, default=datetime.utcnow)
    #: NULL until the line items have been fetched. The refresh calls getOrderItems only
    #: where this is NULL, so re-refreshing 100 known orders costs zero item calls.
    items_fetched_at = Column(DateTime)

    items = relationship(
        "AmazonOrderItem", back_populates="order", lazy="selectin",
        cascade="all, delete-orphan",
    )


class AmazonOrderItem(Base):
    """One line of an Amazon order.

    **The ASIN is the key, not the SellerSKU.** Measured: an order carries
    `SellerSKU: "R-bss 1 kg"`, which is absent from `pricing_data.json`, while its
    `ASIN: "B0G2MKVVB8"` is in the catalogue. Easy Ship SKUs are a different namespace
    from FBA SKUs, so joining on SKU matches nothing and renders every row as an unknown
    product. The SKU is still stored — it is what Amazon's label shows — but it is not
    how the product is identified.
    """
    __tablename__ = "amazon_order_items"
    __table_args__ = (
        Index("idx_amazon_order_items_order_asin", "order_id", "asin"),
    )

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("amazon_orders.id"), nullable=False)
    asin = Column(String(10), nullable=False)
    seller_sku = Column(String(80))
    title = Column(Text)
    quantity_ordered = Column(Integer, default=0)
    quantity_shipped = Column(Integer, default=0)
    item_price = Column(Numeric(12, 2))
    item_tax = Column(Numeric(12, 2))
    promotion_discount = Column(Numeric(12, 2))

    order = relationship("AmazonOrder", back_populates="items")


class OrderPackedEntry(Base):
    """Units the warehouse has packed against one ASIN on one day. **Ours, not Amazon's.**

    The only table in the Orders feature the app writes for itself, and the boundary is
    deliberate: every other row here is a cache of Amazon's data, refreshed rather than
    edited. This is a fact Amazon does not have — how many units are physically in boxes on
    this floor right now — so recording it locally is not a second source of truth about
    whether an order shipped. Nothing here changes an order's status, and no invoice is
    raised from it.

    **Keyed on the DATE, not on the order set.** Measured on production: 200 of 264 orders
    flipped from `PendingPickUp` to `PickedUp` overnight, so a tally attached to
    "not yet collected" orders would erase itself mid-shift the moment the courier arrived.
    A calendar day is stable, and it makes "what did we pack on Monday" answerable.

    `pack_date` is an explicit `String(10)`, matching `ShipmentPackingDay.pack_date` and for
    the same reason: the app runs in IST while `datetime.utcnow` is 5.5 hours behind, so a
    date derived from a timestamp lands on the wrong day for five and a half hours every
    night. The server decides the date from IST and stores it as text.

    UNIQUE on (pack_date, asin), which is what makes a repeated save from a warehouse phone
    an UPDATE rather than a double-count — the same guarantee (day_id, asin) gives packing
    entries.

    Deliberately NOT a copy of `ShipmentPackingDay`: no status lifecycle, no cartons, no hold
    threshold, no submit/verify. Those exist on the shipment side because that data reaches a
    GST invoice. This does not, and unused columns invite a future reader to wire them up.
    """
    __tablename__ = "order_packed_entries"
    __table_args__ = (
        Index("idx_order_packed_date_asin", "pack_date", "asin", unique=True),
    )

    id = Column(Integer, primary_key=True)
    #: IST calendar date, "YYYY-MM-DD". See the class docstring for why it is text.
    pack_date = Column(String(10), nullable=False)
    asin = Column(String(10), nullable=False)
    units = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OrderPackedState(Base):
    """One tick: this ORDER is packed and ready to hand over. **Ours, not Amazon's.**

    Answers a question `OrderPackedEntry` cannot. That table counts units per ASIN per day —
    "how many 500 g pouches are boxed" — which is what the purchasing and SKU views need. It
    cannot answer "is order 407-2831377-6251535 finished", because an order holding two
    different products contributes to two separate ASIN rows and neither one knows the parcel is
    incomplete. Measured on 2026-08-27: 85 orders produced 86 item lines, so one order that day
    needed both its lines packed before it could ship. **The order is the unit that ships**, so
    completeness has to be recorded against the order id.

    Deliberately a BOOLEAN tick, not a status lifecycle. "packed" and "handed to the courier"
    would be two states, and the second is already Amazon's: `easyship_status` moves to
    `PickedUp` when the courier collects, so duplicating it locally would create exactly the
    second source of truth the rest of this feature avoids. What Amazon has no opinion about is
    whether the box is finished on this floor — that, and only that, is stored here.

    **Keyed on (pack_date, amazon_order_id).** `pack_date` for the same reason
    `OrderPackedEntry` carries it: an IST calendar date decided by the SERVER and stored as
    text, because `datetime.utcnow` is 5.5 hours behind and a date derived from a timestamp
    lands on the wrong day every night between 18:30 and 24:00 UTC. UNIQUE on the pair makes a
    repeated tick from a warehouse phone an UPDATE rather than a duplicate row.

    The order id is stored as TEXT, not a foreign key to `amazon_orders.id`. Two reasons, the
    second load-bearing: an Amazon order id is what a barcode carries and what a scanner will
    send, so text is the natural key for that lookup; and `amazon_orders` is a CACHE that
    `purge_older_than` deletes from, so a real FK would either block the purge or cascade the
    warehouse's own record away with it.

    `unpacked_at` is deliberately absent. Un-ticking DELETES the row, so absence means "not
    packed" — one representation of that state rather than two, and no way for a stale timestamp
    to contradict a false flag.
    """
    __tablename__ = "order_packed_state"
    __table_args__ = (
        Index("idx_order_packed_state_date_order", "pack_date", "amazon_order_id", unique=True),
    )

    id = Column(Integer, primary_key=True)
    #: IST calendar date, "YYYY-MM-DD". See the class docstring for why it is text.
    pack_date = Column(String(10), nullable=False)
    #: Amazon's own order id, e.g. "407-2831377-6251535" — what a barcode will carry.
    amazon_order_id = Column(String(20), nullable=False)
    packed_at = Column(DateTime, default=datetime.utcnow)
    #: Who ticked it, so a disputed parcel can be asked about. Blank for a shared login.
    packed_by = Column(String(50))
    #: How the tick arrived: "manual" today, "scan" when the scanner lands. Recorded from the
    #: start so the two can be told apart without a migration later — a scanned tick is evidence
    #: the box was in someone's hand, a typed one is a person's assertion.
    source = Column(String(10), default="manual")


class ProductRawStock(Base):
    """Raw material on hand for one parent product, in kilograms. **Standing, not per-day.**

    Feeds the Orders tab's purchasing view: `to_buy = max(0, ordered_kg - raw_kg)`.

    **No `pack_date`, unlike `OrderPackedEntry`, and the asymmetry is the point.** A packed
    count belongs to a day — it answers "what did we box on the 25th". Raw material on a shelf
    does not vanish at midnight, so a dated row would be blank every morning and the purchasing
    tab would demand 33 numbers be retyped before it meant anything.

    **Keyed on the parent product NAME, not an ASIN.** Raw material is bulk: there is no such
    thing as 500 g-flavoured raw sattu. The name is the catalogue's own `name`, which is the
    key `orders.logic.dispatch_sheet` already groups parents by.

    `Numeric(10, 2)` because this is a weight someone types, and 0.1 kg matters when the total
    reaches a courier. **Callers must convert to `float` before returning it in JSON** —
    SQLAlchemy hands back `Decimal`, which `JSONResponse` cannot serialise, and this app already
    shipped that exact defect once with datetimes.

    Written by hand today. Built to be REPLACED: when the inventory tab exists it writes this
    table instead of a person, and nothing downstream changes.
    """
    __tablename__ = "product_raw_stock"
    __table_args__ = (
        Index("idx_product_raw_stock_product", "product", unique=True),
    )

    id = Column(Integer, primary_key=True)
    #: Parent product name as the MRP catalogue spells it, e.g. "ABC Sattu".
    product = Column(String(120), nullable=False)
    raw_kg = Column(Numeric(10, 2), default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    #: Who typed it, so a surprising number can be asked about.
    updated_by = Column(String(50))


class EconomicsSnapshot(Base):
    """One product's economics for one window, as Amazon reported them. **A cache.**

    Exists so the Portfolio tab renders instantly. A Data Kiosk query takes one to two minutes
    (measured), so a page that fetched on request would hang and two people opening the tab
    would queue behind each other. The background refresh writes these rows; every route reads
    them — the same boundary `amazon_orders` keeps.

    **Nothing here is edited by hand.** A wrong value is fixed by refreshing, not by typing,
    because a local edit would make this a second source of truth about money Amazon has
    already accounted for. The owner's own judgement lives in `ProductDecision` instead.

    UNIQUE on (window_start, window_end, child_asin): a refresh re-run for the same window
    must UPDATE its rows rather than double the portfolio.

    **Fees are a JSON map, not typed columns.** Amazon returned 8 distinct fee types on this
    account (`FbaFulfilmentFee`, `WeightBasedFee`, `FixedClosingFee`, `ReferralFee`,
    `RemovalFee`, `FBAInventoryReimbursement`, `RefundCommissionFee`, `MFNPostageFee`) and adds
    more over time. A column each would mean a migration every time Amazon invents a fee, and
    the dashboard only ever needs the total plus a breakdown on expand.

    Money is `Numeric(12, 2)`, so **callers must convert to `float` before returning it in
    JSON** — SQLAlchemy hands back `Decimal`, which `JSONResponse` cannot serialise. This app
    has already shipped that defect twice (datetimes on the orders payload, then `raw_kg`), so
    `repository.load_snapshot` does the conversion once for every route.
    """
    __tablename__ = "economics_snapshot"
    __table_args__ = (
        Index(
            "idx_economics_snapshot_window_asin",
            # `seller_sku` joined the key when per-SKU rows were added. NULL for the ASIN-level
            # rows that carry the totals, and set for the MSKU breakdown rows that sit beside
            # them — so one table holds both grains without either being able to double the
            # other. `load_snapshot` filters to `seller_sku IS NULL`.
            #
            # SQLite treats NULLs as DISTINCT in a unique index, so this does NOT constrain the
            # ASIN-level rows the way the three-column version did. `save_snapshot` therefore
            # still selects-then-updates rather than relying on the index alone, which it always
            # did — the index is a backstop, not the mechanism.
            "window_start", "window_end", "child_asin", "seller_sku",
            unique=True,
        ),
    )

    id = Column(Integer, primary_key=True)
    #: Window the figures cover, "YYYY-MM-DD". Text for the same reason `pack_date` is:
    #: these are calendar dates from Amazon, not instants, and must not drift with a timezone.
    window_start = Column(String(10), nullable=False)
    window_end = Column(String(10), nullable=False)
    #: The pack size — the level at which a kill decision is taken.
    child_asin = Column(String(10), nullable=False)
    #: **NULL on the authoritative ASIN-level rows; set on the per-SKU breakdown rows.**
    #:
    #: Measured on the live account: 186 of 267 child ASINs sell under TWO merchant SKUs — a
    #: merchant/Easy Ship one and an identically-named "… FBA" one (`0.25 fc np` /
    #: `0.25 fc np FBA`). The dashboard shows them COMBINED, which is what the CHILD_ASIN
    #: aggregation already does; these rows exist only to show the split on expand.
    #:
    #: They are NOT the source of any total. Amazon's MSKU rows lose a little to rows it cannot
    #: attribute to a single SKU, so ASIN-level stays authoritative — verified: merchant
    #: 16,68,051 + FBA 32,81,373 = 49,49,424, matching the ASIN total to the rupee.
    seller_sku = Column(String(80))
    #: The variation family. Not a foreign key: it is Amazon's non-buyable grouping id and
    #: appears in no other table.
    parent_asin = Column(String(10))
    ordered_sales = Column(Numeric(12, 2), default=0)
    refunded_sales = Column(Numeric(12, 2), default=0)
    ad_spend = Column(Numeric(12, 2), default=0)
    net_proceeds = Column(Numeric(12, 2), default=0)
    units_ordered = Column(Integer, default=0)
    units_refunded = Column(Integer, default=0)
    net_units = Column(Integer, default=0)
    #: {feeTypeName: amount} as JSON text. See the class docstring for why it is not columns.
    fees_json = Column(Text)
    #: {adTypeName: amount} as JSON text. One type today (SponsoredProductFee); Sponsored
    #: Brands or Display would appear here without a migration.
    ads_json = Column(Text)
    fetched_at = Column(DateTime, default=datetime.utcnow)


class EconomicsRefresh(Base):
    """When the economics were last pulled, and what they covered. One row per run.

    Kept as history rather than a single overwritten row so "the numbers stopped updating three
    days ago" is answerable. The screen reads the newest row to say how old the figures are —
    the one thing the CSV upload it replaced could never tell anyone.
    """
    __tablename__ = "economics_refresh"

    id = Column(Integer, primary_key=True)
    window_start = Column(String(10))
    window_end = Column(String(10))
    rows_stored = Column(Integer, default=0)
    #: Amazon's own message when a run failed, so a stale dashboard can say why.
    error = Column(Text)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)


class ProjectionRow(Base):
    """One parent product's purchasing forecast row. **Keyed on the parent product NAME, not an
    ASIN** — the same choice `ProductRawStock` makes, for the same reason: this is a purchasing
    decision taken at the parent level, and the MRP sheet's own `name` column is the only stable
    identifier a genuinely new product (Triphala Sattu) carries from day one.

    **`source` is what lets a hand-typed override survive a scheduled recompute.** The weekly job
    only overwrites `sales_source == "sheet"` rows; a `"manual"` row is left alone until the owner
    explicitly resets it. Without this column a refresh would silently discard a manual correction
    the next time it ran — the same failure `ProductDecision` avoids by never being touched by an
    automated pass at all.

    **`needs_review` is set when no entry in `projection_defaults.json` matched this parent's name**
    (case/space/hyphen-insensitive). The row still gets Global Defaults so it is never invisible —
    invisible-because-unconfigured is exactly the Triphala Sattu bug this whole change removes —
    but the owner is told to check the purchase rate and lead times rather than trusting a global
    guess silently.
    """
    __tablename__ = "projection_row"
    __table_args__ = (
        Index("idx_projection_row_parent", "parent_product", unique=True),
    )

    id = Column(Integer, primary_key=True)
    #: The MRP sheet's own product name, unmerged — see the class docstring.
    parent_product = Column(String(120), nullable=False)
    brand = Column(String(60))

    # ── Purchasing config, matched from projection_defaults.json or Global Defaults ──
    purchase_rate = Column(Numeric(10, 2), default=0)
    supplier_to_wh = Column(Integer, default=5)
    packing = Column(Integer, default=2)
    wh_to_ixd = Column(Integer, default=10)
    ixd_to_fba = Column(Integer, default=5)
    wh_buffer_days = Column(Numeric(6, 1), default=10)
    seasonal_impact = Column(Numeric(6, 2), default=1.0)
    #: True when no `projection_defaults.json` entry matched this parent's name. See class docstring.
    needs_review = Column(Boolean, default=False, nullable=False)

    # ── Sales, from economics_snapshot or a manual edit ──
    #: "sheet" (from units_ordered x weight) or "manual" (hand-typed). A "manual" row is skipped by
    #: the weekly recompute.
    sales_source = Column(String(10), nullable=False, default="sheet")
    last_month_sale = Column(Numeric(10, 2), default=0)
    #: kg/day from the LAST 7 DAYS of units_ordered x weight. NULL, never 0.0, when no 7-day
    #: snapshot exists yet for this parent — distinct from a genuine zero-sales week, which is a
    #: real 0.0 and IS blended. See `app.projections.logic.blended_daily_rate`.
    seven_day_rate = Column(Numeric(10, 2))
    #: kg/day from the last 30 days. Always populated once any sheet-sourced row is computed.
    thirty_day_rate = Column(Numeric(10, 2))
    #: The blended rate actually used for the forecast — what `calculate_projections` reads.
    daily_rate = Column(Numeric(10, 2), default=0)
    #: True when |seven_day_rate/thirty_day_rate - 1| exceeded the saved divergence threshold at
    #: the last recompute, so the screen can show WHY a number moved.
    diverged = Column(Boolean, default=False, nullable=False)

    # ── Owner-entered stock and current values, unaffected by the sales source ──
    current_fba_stock = Column(Numeric(10, 1), default=0)
    current_wh_stock = Column(Numeric(10, 1), default=0)

    # Set when the owner removes this parent from the screen. A TIMESTAMP rather than a
    # boolean, matching ShipmentPlanItem.excluded_at exactly and for the same two reasons:
    # `WHERE excluded_at IS NULL` treats every pre-migration row as included with no backfill,
    # and reversibility is the point — removing several rows by mistake is one click back.
    #
    # **This does not permanently retire an active parent.** `build_current_rows` recreates a
    # bare row for any currently-active sheet parent missing one, and exclusion does not stop
    # that — only a row for a parent no longer active in the sheet stays hidden for good.
    excluded_at = Column(DateTime)

    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(String(60))


class ProjectionRefresh(Base):
    """When the weekly 7d/30d sales blend was last recomputed, and what it covered. One row per
    run — the same shape as `EconomicsRefresh`, so "the numbers stopped updating" is answerable
    the same way on this tab as on the Portfolio tab.
    """
    __tablename__ = "projection_refresh"

    id = Column(Integer, primary_key=True)
    window_start = Column(String(10))
    window_end = Column(String(10))
    rows_stored = Column(Integer, default=0)
    error = Column(Text)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)


class ProductDecision(Base):
    """The owner's decision about one parent product: kill, keep or watch. **Ours, not Amazon's.**

    The counterpart to `EconomicsSnapshot`: that table is what Amazon says, this is what the
    owner concluded. Keeping them apart is what makes next month's question answerable — "I
    marked Moori KILL on 27 Aug at -56.8% net; what is it now?" — which the old
    `discontinued_products.json` could not answer, because it stored a name in a set with no
    date, no reason and no numbers.

    **Keyed on the PARENT asin**, because that is the level a decision is taken at: you stop
    selling a product, or you stop selling one of its sizes. `SURGICAL` is recorded here as the
    parent's verdict and the note names the sizes.

    A decision is never applied automatically. Nothing in this app turns ads off or delists a
    product; the tick records a judgement so it can be revisited, and Seller Central remains
    the only place the action happens.
    """
    __tablename__ = "product_decision"
    __table_args__ = (
        Index("idx_product_decision_parent", "parent_asin", unique=True),
    )

    id = Column(Integer, primary_key=True)
    parent_asin = Column(String(10), nullable=False)
    #: "kill" | "keep" | "watch". Free text rather than an enum so a new category needs no
    #: migration; the route validates against a tuple.
    decision = Column(String(10), nullable=False)
    #: Why, in the owner's words. The most valuable column here in three months' time.
    note = Column(Text)
    #: The figures at the moment of the decision, so a later review can compare against them
    #: rather than trusting memory. JSON for the same reason `fees_json` is.
    snapshot_json = Column(Text)
    decided_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    decided_by = Column(String(50))


class AdsSnapshot(Base):
    """Ad spend against ATTRIBUTED sales, per SKU, for one window. **Amazon's, cached.**

    A separate table from `EconomicsSnapshot` because it comes from a separate API with a
    different grain and a different failure mode. The Advertising API report takes ~12 minutes to
    generate (measured) against the economics query's 30 seconds, so the two are fetched in
    separate phases and **an ads failure must not cost the margins**. Two tables make that
    trivially true.

    **What it adds that SP-API cannot: `attributed_sales`.** The Economics feed reports the ad
    CHARGE, which gives TACOS (spend / total sales). It has no attributed-sales column, so true
    ACOS (spend / sales the ads actually caused) is not derivable from it. Measured 27 Jul –
    26 Aug: TACOS 33.1% against a true ACOS of **89.9%** — Rs 1 of ads returning Rs 1.11. Those
    are different claims about the same money and the tab shows both.

    **Keyed on the SELLER SKU, not just the ASIN**, because that is the grain Amazon reports and
    because it carries the fulfilment channel: `0.25 fc np` versus `0.25 fc np FBA`. Measured, the
    merchant SKU of `B0DCCL1531` spent Rs 1,444 for ZERO attributed sales while its FBA twin
    returned 36% ACOS — one number per ASIN would have hidden that.

    Money is `Numeric(12, 2)`; **callers convert to float before JSON**, as everywhere in this
    app (`JSONResponse` cannot serialise `Decimal`).
    """
    __tablename__ = "ads_snapshot"
    __table_args__ = (
        Index(
            "idx_ads_snapshot_window_asin_sku",
            "window_start", "window_end", "child_asin", "seller_sku",
            unique=True,
        ),
    )

    id = Column(Integer, primary_key=True)
    window_start = Column(String(10), nullable=False)
    window_end = Column(String(10), nullable=False)
    child_asin = Column(String(10), nullable=False)
    #: Amazon's `advertisedSku`. Verified: all 213 advertised SKUs join to the economics MSKUs.
    seller_sku = Column(String(80), nullable=False, default="")
    cost = Column(Numeric(12, 2), default=0)
    #: `attributedSalesSameSku14d` — sales Amazon credits to a click on this SKU within 14 days.
    attributed_sales = Column(Numeric(12, 2), default=0)
    purchases = Column(Integer, default=0)
    clicks = Column(Integer, default=0)
    impressions = Column(Integer, default=0)
    fetched_at = Column(DateTime, default=datetime.utcnow)


class PortfolioSettings(Base):
    """The owner's verdict thresholds. **One JSON row, not a column per number.**

    The verdict rules ship with values measured from this account (net >= 25% is healthy here,
    TACOS <= 30% is efficient here, the account averages 29% and 33%). Those defaults are
    evidence, not preference — but they are still thresholds someone should be able to argue
    with, so they are editable and saved.

    JSON rather than typed columns for the same reason `fees_json` is: adding a rule would
    otherwise need a migration, and the rules are the part of this feature most likely to change
    as the owner learns what he wants from it. `repository.load_settings` validates the keys
    against `logic.DEFAULT_THRESHOLDS`, so an unknown key is refused rather than silently doing
    nothing.

    `name` exists so a second saved set (a stricter one for a bad month, say) needs no schema
    change. Today there is exactly one row, `"thresholds"`.
    """
    __tablename__ = "portfolio_settings"
    __table_args__ = (
        Index("idx_portfolio_settings_name", "name", unique=True),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(40), nullable=False, default="thresholds")
    value_json = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(50))


class AdsEntity(Base):
    """One campaign, ad group, keyword or targeting clause. **Amazon's, cached.**

    ONE table for four entity types rather than four tables, because the Ads tab treats them as a
    hierarchy to walk and a bid to edit: every query is "the children of X" or "the current bid of
    Y". Four tables would need three joins to render one screen and would repeat the same five
    columns four times.

    **The bid lives here AND at Amazon, and Amazon wins.** Before a run is applied the bid is
    re-read for the rows being changed, because a bid edited in Seller Central since the last
    refresh would otherwise be silently overwritten with a percentage of a stale number.

    Sizes are measured, not guessed: 24 campaigns, 2,542 ad groups, 148,291 keywords and 200,000+
    targeting clauses on this account, over 9 minutes to page in full. **So the entity list is
    never fully cached** — only rows a report or a rule has touched.

    `entity_id` is Amazon's id as TEXT. They are 15-digit numbers that would fit a bigint today,
    but Amazon documents them as opaque strings and a numeric column would lose a leading zero or
    choke on a future non-numeric id. Verified the keyword and target id spaces do not overlap
    (0 collisions in a 1,000-id sample), so `(entity_type, entity_id)` is a safe key.
    """
    __tablename__ = "ads_entity"
    __table_args__ = (
        Index("idx_ads_entity_type_id", "entity_type", "entity_id", unique=True),
        Index("idx_ads_entity_parent", "entity_type", "parent_id"),
        Index("idx_ads_entity_campaign", "campaign_id"),
    )

    id = Column(Integer, primary_key=True)
    #: "campaign" | "ad_group" | "keyword" | "target"
    entity_type = Column(String(12), nullable=False)
    #: "sp" (Sponsored Products) | "sb" (Sponsored Brands). **A first-class dimension, not a
    #: boolean**, because Sponsored Display is a plausible third and adding it should be a fetch
    #: plus a writer rather than a redesign. `EXACT` is a legal match type on both products, so the
    #: row itself cannot say which endpoint owns it — see `ads.logic.writer_for`.
    ad_product = Column(String(4), nullable=False, default="sp", server_default="sp")
    entity_id = Column(String(32), nullable=False)
    #: The immediate parent: a campaign for an ad group, an ad group for a keyword or target.
    parent_id = Column(String(32))
    #: Denormalised so "everything in this campaign" is one indexed query rather than a recursive
    #: walk. A keyword's campaign cannot change without the keyword being recreated, so this
    #: cannot drift the way a cached total could.
    campaign_id = Column(String(32))
    name = Column(String(500))
    state = Column(String(12))
    #: `EXACT`/`PHRASE`/`BROAD` for keywords, `TARGETING_EXPRESSION*` for targeting clauses.
    #: **This decides which endpoint a write goes to** (`ads.logic.writer_for`). The report labels
    #: both id columns `keywordId`, so without this a targetId reaches `/sp/keywords`, which
    #: answers 207 with the failure buried in an `error` array.
    match_type = Column(String(40))
    #: NULL for a target that inherits its ad group's `default_bid`, and kept distinct from 0.0:
    #: `logic.new_bid` refuses to take a percentage of an inherited bid, because writing one
    #: converts the target from inheriting to fixed.
    bid = Column(Numeric(12, 2))
    #: Ad groups only. What an inheriting child actually spends.
    default_bid = Column(Numeric(12, 2))
    #: Campaigns only, so the tab can show what a bid change is competing for.
    daily_budget = Column(Numeric(12, 2))
    fetched_at = Column(DateTime, default=datetime.utcnow)


class AdsPerformanceDaily(Base):
    """Spend and sales per entity **per DAY**. Amazon's, cached — and the reason any date range is
    instant.

    **The ONLY grain, and that is the point.** There used to be an `ads_performance` table beside
    this one holding a row per entity per WINDOW, and the two disagreed by **28% of spend**: the
    refresh wrote Sponsored Brands to the window table and not to this one, so any range nobody had
    fetched exactly under-reported by Rs 1,26,328 and a bid rule on it silently omitted 296 live SB
    keywords. Deleting that table is what makes one answer possible rather than two.

    Daily rows are summable, which is what makes "I have 60 days, show me 20 of them" free: any range
    inside what we hold is a `GROUP BY entity_id` away, with no Amazon call and no ~20-minute report.

    Measured before choosing this over prefetching fixed presets:

        DAILY report, 7 days      45,650 rows   (SUMMARY: 12,854)
        bulk insert throughput    30,921 rows/sec
        30 days of daily rows     ~195,000 rows -> 6 SECONDS to store, ~56 MB

    The per-row SELECT-then-UPDATE upsert used everywhere else in this app runs at 498 rows/sec and
    would need 6.5 MINUTES for the same data. So this table is written by **delete-then-bulk-insert
    per day**, which is the one place in the codebase that deviates from the house upsert — because
    a day's rows are wholly replaced by a refetch, never merged, so there is nothing an upsert would
    preserve.

    **Kept to a 60-DAY rolling window, purged nightly**, matching what the nightly scrape fetches so
    every range the tab offers is answerable from stored rows. Measured at 8,384 rows/day in July
    (August is quieter at 6,107), so 60 days is ~503,000 rows and ~93 MB. Production sits at 87%
    disk and `update-ec2.sh` copies the whole database before every deploy, which is why the bound
    exists at all — and why `KEEP_BACKUPS` dropped from 5 to 3 in the same change.

    Keyed `(day, entity_id, ad_product)`: one row per entity per day per ad product. `day` is a
    plain `String(10)` date in the marketplace's own reporting timezone — never a `DateTime`,
    because a timezone conversion on a bare date is how the Orders tab once rendered a date as
    05:30 the following morning.

    **`ad_product` is in the unique key, and it has to be.** The key was `(day, entity_id)` while
    only Sponsored Products was ever stored here. Sponsored Brands is a separate API with a separate
    id space, so a colliding id would make the two products' rows for one day mutually exclusive —
    the second insert would fail, or worse, be the only one kept. 0 collisions across 29,360 ids
    today is luck, not a guarantee.
    """
    __tablename__ = "ads_performance_daily"
    __table_args__ = (
        Index("idx_ads_daily_day_entity", "day", "entity_id", "ad_product", unique=True),
        # The index the sub-range sum reads: bounded by day, grouped by entity.
        Index("idx_ads_daily_day", "day"),
        Index("idx_ads_daily_campaign", "day", "campaign_id"),
        # Every write deletes by (day, ad_product) and `daily_range_complete` asks which days a
        # product holds, so both paths want this.
        Index("idx_ads_daily_day_product", "day", "ad_product"),
    )

    id = Column(Integer, primary_key=True)
    #: `YYYY-MM-DD` as Amazon reported it.
    day = Column(String(10), nullable=False)
    entity_id = Column(String(32), nullable=False)
    entity_type = Column(String(12), nullable=False, default="target")
    ad_product = Column(String(4), nullable=False, default="sp", server_default="sp")
    campaign_id = Column(String(32))
    ad_group_id = Column(String(32))
    text = Column(String(500))
    match_type = Column(String(40))
    #: The bid as reported on that DAY. A sub-range sum takes the LATEST day's bid, not a sum —
    #: adding bids across days would produce a number that means nothing.
    reported_bid = Column(Numeric(12, 2))
    impressions = Column(Integer, default=0)
    clicks = Column(Integer, default=0)
    spend = Column(Numeric(12, 2), default=0)
    orders = Column(Integer, default=0)
    sales = Column(Numeric(12, 2), default=0)
    fetched_at = Column(DateTime, default=datetime.utcnow)


class AdsRule(Base):
    """A saved bid rule: conditions plus an action. **The owner's, not Amazon's.**

    JSON rather than columns for the conditions, for the same reason `portfolio_settings` is JSON:
    the rule vocabulary is the part of this feature most likely to grow, and a new field would
    otherwise need a migration. `logic.condition_error` validates every condition before a run, so
    an unknown field is refused rather than quietly matching nothing.

    Saved rules are a convenience, **not a schedule.** Nothing here runs a rule automatically:
    every run is a human pressing Preview and then Apply. A screen that could move 299 live bids on
    a timer is one that moves them on a bad data day — the same reason the Portfolio tab never
    auto-applies a verdict.
    """
    __tablename__ = "ads_rule"
    __table_args__ = (
        Index("idx_ads_rule_name", "name", unique=True),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    #: `[{"field": "spend", "op": "gt", "value": 100}, ...]`, ANDed.
    conditions_json = Column(Text)
    action = Column(String(20))
    amount = Column(Numeric(12, 2))
    #: Window in days. 7/14/30 are single reports and attribution-exact; above 31 Amazon needs
    #: several reports (its measured per-report cap).
    window_days = Column(Integer, default=7)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_run_at = Column(DateTime)


class AdsMutation(Base):
    """One bid change sent to Amazon, with the value it held BEFORE. **Ours, and the audit trail.**

    **`old_bid` is written before the request is sent, and it is what makes this feature safe.** A
    rule changes several hundred bids in one click — the owner's own rule matched 299 rows carrying
    Rs 102,945 of weekly spend — and Amazon has no undo. Without the previous value stored,
    reversing a mistaken run means reading 299 numbers off a report that has already moved on. With
    it, undo is just another bulk write. Same lesson as the 400 units of packed stock that survived
    only because `update-ec2.sh` backs up before every deploy.

    **`status` distinguishes real outcomes, because `207 Multi-Status` makes partial failure
    NORMAL.** Measured: `PUT /sp/keywords` returns `{"success": [...], "error": [...]}`, and a bid
    under the marketplace minimum comes back as a `rangeError` for that row alone while every other
    row succeeds. Treating 207 as success is how a failed edit becomes invisible.

    * `pending` — written, not yet sent. A crash mid-run leaves these, which is the point: they name
      exactly what was in flight.
    * `applied` — Amazon confirmed it in the `success` array.
    * `failed` — Amazon refused it; `error` carries their own message.
    * `reverted` — an undo has since restored `old_bid`.

    `run_id` groups a whole application so undo operates on the unit the owner recognises ("the run
    I did at 3pm") rather than on individual rows.
    """
    __tablename__ = "ads_mutation"
    __table_args__ = (
        Index("idx_ads_mutation_run", "run_id"),
        Index("idx_ads_mutation_entity", "entity_id"),
        Index("idx_ads_mutation_run_entity", "run_id", "entity_id", unique=True),
        # The bid log filters by date and the once-per-day guard asks "what did we change today", so
        # both scan this column. The table is now expected to hold a YEAR of runs — at three 1,000-row
        # runs a day that is ~1,000,000 rows, where an unindexed range scan is what turns a page that
        # loads into a page that times out.
        Index("idx_ads_mutation_created", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    #: A uuid4 per Apply, not an autoincrement: it is minted before any row is written so every row
    #: of a run can carry it, and it appears in the URL of an undo.
    run_id = Column(String(36), nullable=False)
    entity_id = Column(String(32), nullable=False)
    entity_type = Column(String(12), nullable=False, default="keyword")
    #: "sp" | "sb". Recorded so the audit trail says which API was written to, not merely which
    #: entity — an undo must go back to the same endpoint with the same payload shape.
    ad_product = Column(String(4), nullable=False, default="sp", server_default="sp")
    #: "keyword", "target" or "sb_keyword" — WHICH ENDPOINT this row was sent to. Recorded rather
    #: than re-derived
    #: so a misrouted write is visible in the ledger after the fact.
    writer = Column(String(12), nullable=False, default="keyword")
    text = Column(String(500))
    campaign_id = Column(String(32))
    ad_group_id = Column(String(32))
    old_bid = Column(Numeric(12, 2))
    new_bid = Column(Numeric(12, 2))
    status = Column(String(12), nullable=False, default="pending")
    #: Amazon's own refusal, verbatim. Their validation messages name the cause — they are how the
    #: 31-day report cap and the bid floor were both found — so they are surfaced, not replaced.
    error = Column(Text)
    #: The rule that produced this row, in words, so the ledger reads without joining to a rule
    #: that may since have been edited or deleted.
    rule_summary = Column(String(300))
    #: Set when this row was created BY an undo, naming the run being reversed — so a double-undo
    #: is detectable and the history reads as a chain rather than a loop.
    reverts_run_id = Column(String(36))
    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime)


class AdsRefresh(Base):
    """When the ads figures were last pulled, what they covered, and **what only PARTLY worked**.

    A sibling of `EconomicsRefresh`, and it exists because that table's absence here cost a morning.

    On 1 Sep the nightly run stored 482,578 Sponsored Products rows across 59 days and Amazon then
    rate-limited the Sponsored Brands report, storing **0**. `refresh.run` recorded that correctly —
    in a module-level `STATE` dict. The app was then restarted by a deploy, the dict reset, and the
    Ads tab reported "nothing fetched" with no way to learn that half a million current rows were
    sitting in the table and one throttled report was the whole problem. The reason a screen is empty
    must outlive the process that discovered it.

    **`sp_rows` and `sb_rows` are separate columns, not one total.** `0 SB` beside `482,578 SP` IS the
    finding; a single `rows_stored` of 482,578 reads as a completely successful night. Same reasoning
    as `fees` being a JSON map on `EconomicsSnapshot` — the shape has to be able to express the
    failure that actually happens.
    """
    __tablename__ = "ads_refresh"

    id = Column(Integer, primary_key=True)
    window_start = Column(String(10))
    window_end = Column(String(10))
    #: `done` · `partial` · `failed`. **`partial` is the state this table was added for**: a run where
    #: one ad product's report landed and another's was throttled. Recorded as its own word rather
    #: than inferred from `sb_error` being set, so a future product cannot make the inference wrong.
    status = Column(String(10), nullable=False, default="done")
    sp_rows = Column(Integer, default=0)
    sb_rows = Column(Integer, default=0)
    campaigns = Column(Integer, default=0)
    ad_groups = Column(Integer, default=0)
    #: The whole run failed — no figures moved.
    error = Column(Text)
    #: **Sponsored Brands alone failed, and Sponsored Products is current.** Kept apart from `error`
    #: because the two call for different sentences on screen: "the refresh failed" is wrong when 72%
    #: of the spend was updated successfully.
    sb_error = Column(Text)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)

import asyncio
import csv
import functools
import io
import json
import os
import re
import threading
import urllib.request
from datetime import datetime

from flask import (Flask, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
import pandas as pd
from sqlalchemy import Column, DateTime, Integer, Text, create_engine
from sqlalchemy.orm import declarative_base, Session as DBSession

from scraper import scrape_all

# ── App config ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())
APP_PASSWORD  = os.environ.get("APP_PASSWORD", "admin123")   # change via env var

# ── Database ──────────────────────────────────────────────────────────────────
_db_url = os.environ.get("DATABASE_URL", "sqlite:///tracker.db")
# Railway / Heroku emit "postgres://" — SQLAlchemy needs "postgresql://"
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)

Base = declarative_base()

class Snapshot(Base):
    __tablename__ = "snapshots"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    results    = Column(Text,    nullable=False)   # JSON array of result dicts
    asin_count = Column(Integer)
    scraped_at = Column(DateTime, default=datetime.utcnow)

engine = create_engine(_db_url, pool_pre_ping=True)
Base.metadata.create_all(engine)


def save_snapshot(results):
    """Overwrite the single stored snapshot with the latest results."""
    with DBSession(engine) as db:
        db.query(Snapshot).delete()
        db.add(Snapshot(results=json.dumps(results, ensure_ascii=False),
                        asin_count=len(results)))
        db.commit()


def load_snapshot():
    """Return (results_list, formatted_timestamp) of the last stored snapshot."""
    try:
        with DBSession(engine) as db:
            snap = db.query(Snapshot).order_by(Snapshot.id.desc()).first()
            if snap:
                ts = snap.scraped_at.strftime("%d %b %Y, %I:%M %p") if snap.scraped_at else ""
                return json.loads(snap.results), ts
    except Exception:
        pass
    return [], ""


# ── In-memory scrape state ────────────────────────────────────────────────────
state = {
    "running":        False,
    "progress":       0,
    "total":          0,
    "current_asin":   "",
    "results":        [],
    "last_scraped_at": "",
    "error":          None,
}

# Pre-load last fetch from DB so results survive restarts
state["results"], state["last_scraped_at"] = load_snapshot()

# ── Column order for Excel exports ───────────────────────────────────────────
COLS = ["ASIN", "URL", "Title", "Rating", "No. of Ratings", "BSR",
        "Buybox Price", "Buybox Seller", "Buybox Fulfillment", "Other Sellers",
        "Limited Time Deal", "Use By Date", "Status"]

# Google Sheet config — column I = index 8
SHEET_ASIN_COL = 8


# ── Auth helpers ──────────────────────────────────────────────────────────────
def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Wrong password. Try again."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Helpers ───────────────────────────────────────────────────────────────────
def make_excel(rows):
    df = pd.DataFrame(rows)
    df = df[[c for c in COLS if c in df.columns]]
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Amazon Tracker")
        ws = writer.sheets["Amazon Tracker"]
        for col in ws.columns:
            w = max((len(str(c.value)) for c in col if c.value), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(w + 4, 60)
    out.seek(0)
    return out


def run_scrape_thread(asins):
    def progress_callback(done, total, asin):
        state["progress"] = done
        state["total"]    = total
        state["current_asin"] = asin

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        results = loop.run_until_complete(scrape_all(asins, progress_callback))
        state["results"] = results
        state["progress"] = state["total"]
        # Persist to DB so results survive restarts
        save_snapshot(results)
        state["last_scraped_at"] = datetime.utcnow().strftime("%d %b %Y, %I:%M %p")
    except Exception as e:
        state["error"] = str(e)
    finally:
        state["running"] = False
        loop.close()


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/scrape", methods=["POST"])
@login_required
def scrape():
    if state["running"]:
        return jsonify({"error": "Scraping already in progress"}), 400
    data  = request.get_json()
    asins = [a.strip() for a in data.get("asins", []) if a.strip()]
    if not asins:
        return jsonify({"error": "No ASINs provided"}), 400
    state.update(running=True, progress=0, total=len(asins),
                 current_asin="", results=[], error=None)
    threading.Thread(target=run_scrape_thread, args=(asins,), daemon=True).start()
    return jsonify({"status": "started", "total": len(asins)})


@app.route("/progress")
@login_required
def progress():
    return jsonify({k: state[k] for k in
                    ("running", "progress", "total", "current_asin",
                     "error", "last_scraped_at")})


@app.route("/results")
@login_required
def results():
    return jsonify(state["results"])


@app.route("/download")
@login_required
def download():
    if not state["results"]:
        return jsonify({"error": "No results"}), 400
    return send_file(make_excel(state["results"]),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="amazon_tracker.xlsx")


@app.route("/download-filtered", methods=["POST"])
@login_required
def download_filtered():
    rows = request.get_json().get("rows", [])
    if not rows:
        return jsonify({"error": "No rows"}), 400
    return send_file(make_excel(rows),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="amazon_tracker_filtered.xlsx")


@app.route("/fetch-sheet", methods=["POST"])
@login_required
def fetch_sheet():
    """Fetch ASINs from a public Google Sheet (column I = index 8)."""
    data      = request.get_json()
    sheet_url = (data.get("url") or "").strip()
    if not sheet_url:
        return jsonify({"error": "No URL provided"}), 400

    id_m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', sheet_url)
    if not id_m:
        return jsonify({"error": "Invalid Google Sheets URL"}), 400
    sheet_id = id_m.group(1)
    gid_m    = re.search(r'[#&?]gid=(\d+)', sheet_url)
    gid      = gid_m.group(1) if gid_m else "0"

    export_url = (f"https://docs.google.com/spreadsheets/d/{sheet_id}"
                  f"/export?format=csv&gid={gid}")
    try:
        req = urllib.request.Request(export_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8")
    except Exception as e:
        return jsonify({"error": f"Could not fetch sheet: {e}"}), 500

    reader = csv.reader(io.StringIO(content))
    asins  = []
    for i, row in enumerate(reader):
        if i == 0:
            continue
        if len(row) > SHEET_ASIN_COL:
            val = row[SHEET_ASIN_COL].strip().upper()
            if re.match(r'^[A-Z0-9]{10}$', val):
                asins.append(val)

    if not asins:
        return jsonify({"error": "No valid ASINs found in column I"}), 400
    return jsonify({"asins": asins, "count": len(asins)})


if __name__ == "__main__":
    print("Amazon Tracker → http://localhost:5000")
    app.run(debug=False, port=5000)

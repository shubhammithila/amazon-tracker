#!/usr/bin/env bash
#
# Update the running app on EC2, safely.
#
#   ssh -i your-key.pem ubuntu@13.233.144.148
#   cd /opt/amazon-tracker
#   ./deploy/update-ec2.sh
#
# What this does, in order, and why each step is here:
#
#   1. Backs up tracker.db FIRST. The box holds real scraped product history and real
#      GST invoices with legally-sequential numbers. Everything below is recoverable
#      from this file, and nothing below runs if the backup fails.
#   2. Records the current commit, so a rollback is one `git checkout` away.
#   3. Fetches and checks out the requested ref.
#   4. Installs dependencies.
#   5. Runs `alembic upgrade head` — BEFORE restarting. This is the step whose omission
#      is already written up in DEPLOY.md: the app's create_all() only ever creates
#      MISSING TABLES, it never adds a column to a table that already exists. Skipping
#      it means the app starts, looks fine, and 500s the pages whose columns are absent.
#   6. Restarts and then VERIFIES over HTTP.
#   7. On any failure, rolls the code back, re-restarts, and tells you where the backup
#      is. A half-deployed app is worse than the old one.
#
# Deliberately NOT automatic on push. This app has one operator and a shared password
# still in play; a deploy should be a decision someone makes, not a side effect of a
# commit.

set -Eeuo pipefail

# ── Settings ────────────────────────────────────────────────────────────────
APP_DIR="${APP_DIR:-/opt/amazon-tracker}"
SERVICE="${SERVICE:-tracker}"
BRANCH="${1:-claude/stoic-allen-bb3a55}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/tracker-backups}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
KEEP_BACKUPS="${KEEP_BACKUPS:-10}"

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/tracker-$STAMP.db"
PREVIOUS_REF=""
RESTORE_NEEDED=0

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[0;32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[0;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[0;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── Rollback ────────────────────────────────────────────────────────────────
# Runs on ANY error after the checkout, via trap. Restores the code and restarts, so
# the box is never left serving a half-updated app.
#
# It does NOT automatically restore the database. A migration that partially applied
# needs a human to look at it, and silently reverting to a backup would discard any
# packing entered between the backup and the failure. The path is printed instead.
rollback() {
  local code=$?
  [ "$code" -eq 0 ] && return 0

  printf '\n\033[0;31m✗ Deploy failed (exit %s)\033[0m\n' "$code" >&2

  if [ -n "$PREVIOUS_REF" ]; then
    warn "Rolling the code back to $PREVIOUS_REF"
    git -C "$APP_DIR" checkout --quiet "$PREVIOUS_REF" 2>/dev/null \
      && ok "code restored" \
      || warn "could not check out $PREVIOUS_REF — do it by hand"
    sudo systemctl restart "$SERVICE" 2>/dev/null \
      && ok "service restarted on the old code" \
      || warn "could not restart $SERVICE — check: sudo journalctl -u $SERVICE -n 50"
  fi

  if [ "$RESTORE_NEEDED" -eq 1 ]; then
    cat >&2 <<EOF

    The database MAY have been migrated before the failure. The old copy is at:

        $BACKUP_FILE

    Restore it ONLY if the app is broken, and know that anything entered since this
    deploy started will be lost:

        sudo systemctl stop $SERVICE
        cp "$BACKUP_FILE" "$APP_DIR/tracker.db"
        sudo systemctl start $SERVICE
EOF
  fi
  exit "$code"
}

# ── Checks before touching anything ─────────────────────────────────────────
say "Checking the box"
[ -d "$APP_DIR" ] || die "$APP_DIR does not exist. Is this the right server?"
cd "$APP_DIR"
[ -d .git ] || die "$APP_DIR is not a git checkout — this box was deployed by unpacking
    a tarball. Use the tarball instructions in DEPLOY.md instead, or convert it:
        cd $APP_DIR && git init && git remote add origin <repo> && git fetch && git checkout -f $BRANCH"
[ -x venv/bin/python ] || die "no venv at $APP_DIR/venv — run deploy/setup-ec2.sh first"
command -v sudo >/dev/null || die "sudo is required to restart $SERVICE"
ok "app dir, venv and git checkout all present"

# Uncommitted changes on the server usually mean somebody edited a file in place, and
# the checkout below would overwrite them silently.
#
# On this box that is not hypothetical: app/invoice/hsn_master.json is a TRACKED file
# that the app WRITES TO at runtime (verified HSN codes are appended after each
# invoice). It had grown from 15 entries in git to 87 on the server — 72 hand-verified
# GST classifications that a plain checkout would have thrown away.
#
# So local changes are stashed rather than discarded, and the stash reference is printed.
# A stash is recoverable; `checkout -f` is not.
if ! git diff --quiet || ! git diff --cached --quiet; then
  warn "There are uncommitted changes in $APP_DIR:"
  git status --short | sed 's/^/      /'
  echo
  warn "These will be STASHED (not deleted) so the checkout cannot lose them."
  read -r -p "    Continue? [y/N] " reply
  [ "$reply" = "y" ] || [ "$reply" = "Y" ] || die "Stopped. Nothing was changed."
  git stash push --include-untracked -m "update-ec2 $STAMP" >/dev/null \
    && ok "stashed as: git stash list | head -1   (restore a file with: git checkout stash@{0} -- <path>)" \
    || warn "nothing to stash"
  STASHED=1
fi

# Runtime data files the APP writes into its own tracked tree. After the checkout these
# are restored from the stash, because git's copy is a stale snapshot and the server's is
# the live record. Listed explicitly rather than guessed: each one is a deliberate
# decision, and a wrong entry here silently reverts real data.
RUNTIME_DATA_FILES="app/invoice/hsn_master.json"

# ── 1. Back up the database FIRST ───────────────────────────────────────────
say "Backing up the database"
mkdir -p "$BACKUP_DIR"
if [ -f tracker.db ]; then
  # Python's sqlite3.backup(), not `cp`, and not the sqlite3 CLI.
  #
  # `cp` of a live database can capture a torn write if the app is mid-transaction — and
  # the scheduler on this box writes at 06:00 daily, so "the app is idle" is an
  # assumption, not a fact. The backup API takes a consistent snapshot of a database
  # that is in use.
  #
  # The sqlite3 CLI is NOT installed on this server (checked), so relying on it would
  # have silently downgraded every backup to `cp`. Python is guaranteed present — it is
  # what runs the app.
  venv/bin/python - "$BACKUP_FILE" <<'PY' || die "backup failed — stopping. Nothing was changed."
import sqlite3, sys
source = sqlite3.connect("tracker.db")
target = sqlite3.connect(sys.argv[1])
with target:
    source.backup(target)
# Verify BEFORE anything relies on it: an unreadable backup is worse than none,
# because it is trusted.
check = target.execute("pragma integrity_check").fetchone()[0]
if check != "ok":
    print(f"    integrity_check said: {check}")
    sys.exit(1)
tables = target.execute(
    "select count(*) from sqlite_master where type='table'"
).fetchone()[0]
rows = 0
for (name,) in target.execute("select name from sqlite_master where type='table'"):
    rows += target.execute(f'select count(*) from "{name}"').fetchone()[0]
print(f"    verified: {tables} tables, {rows} rows, integrity_check ok")
PY
  ok "saved $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"
else
  warn "no tracker.db yet — nothing to back up (first deploy?)"
fi

# Prune old backups, keeping the most recent KEEP_BACKUPS. An 8 GB disk fills up.
if [ -d "$BACKUP_DIR" ]; then
  # shellcheck disable=SC2012
  ls -1t "$BACKUP_DIR"/tracker-*.db 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) \
    | xargs -r rm -f
fi

# ── 2. Remember where we are ────────────────────────────────────────────────
say "Recording the current version"
PREVIOUS_REF="$(git rev-parse HEAD)"
ok "currently on $(git rev-parse --short HEAD) ($(git log -1 --format=%s | cut -c1-60))"
trap rollback EXIT

# ── 3. Fetch and check out ──────────────────────────────────────────────────
say "Fetching $BRANCH"
git fetch --all --tags --prune
git checkout --quiet -B deploy-current "origin/$BRANCH" \
  || die "could not check out origin/$BRANCH"
ok "now on $(git rev-parse --short HEAD) ($(git log -1 --format=%s | cut -c1-60))"

# Put the live runtime data back over git's stale snapshot.
if [ "${STASHED:-0}" -eq 1 ]; then
  for f in $RUNTIME_DATA_FILES; do
    if git cat-file -e "stash@{0}:$f" 2>/dev/null; then
      git checkout "stash@{0}" -- "$f" 2>/dev/null \
        && ok "restored live $f from the stash" \
        || warn "could not restore $f — check: git checkout stash@{0} -- $f"
    fi
  done
fi

# ── 4. Dependencies ─────────────────────────────────────────────────────────
# **Only install what is MISSING. Never force a version.**
#
# This box runs Python 3.14 and already has newer versions than requirements.txt pins
# (pandas 3.0.3 vs 2.2.3, fastapi 0.136 vs 0.115, and so on). `pip install -r` therefore
# tried to DOWNGRADE pandas — and pandas 2.2.3 has no wheel for 3.14, so pip compiled it
# from source and the 951 MB box OOM-killed the compile ("code=137, Killed"). The deploy
# failed and rolled back, having achieved nothing.
#
# The pins in requirements.txt are a floor for a fresh install, not a target for an
# existing one. Downgrading a working server to match a lockfile is the wrong direction:
# it risks breaking what already runs in order to satisfy a number in a file.
#
# So: import-check each dependency, install only the absent ones, and never build from
# source on this box. --only-binary=:all: makes a missing wheel a clear failure instead
# of a 20-minute compile that ends in an OOM kill.
say "Checking dependencies"
MISSING="$(venv/bin/python - <<'PY'
import importlib.util
# (import name, pip name). They differ often enough that guessing gets it wrong.
required = [
    ("fastapi", "fastapi"), ("uvicorn", "uvicorn[standard]"),
    ("sqlalchemy", "sqlalchemy[asyncio]"), ("aiosqlite", "aiosqlite"),
    ("alembic", "alembic"), ("pydantic_settings", "pydantic-settings"),
    ("httpx", "httpx"), ("lxml", "lxml"), ("pandas", "pandas"),
    ("openpyxl", "openpyxl"), ("apscheduler", "apscheduler"),
    ("multipart", "python-multipart"), ("itsdangerous", "itsdangerous"),
    ("jinja2", "jinja2"), ("aiofiles", "aiofiles"), ("reportlab", "reportlab"),
]
print(" ".join(pip for mod, pip in required if importlib.util.find_spec(mod) is None))
PY
)"

if [ -n "$MISSING" ]; then
  warn "installing missing package(s): $MISSING"
  # shellcheck disable=SC2086
  venv/bin/pip install --quiet --only-binary=:all: $MISSING || die "could not install: $MISSING
    (--only-binary is deliberate: building from source on a 951 MB box gets OOM-killed.
     If a package genuinely has no wheel for this Python, install it by hand and re-run.)"
  ok "installed: $MISSING"
else
  ok "every dependency is already present — nothing to install"
fi

# The app must actually import with what is installed. A newer library than the pin can
# have removed something the code uses, and finding that out from a 500 after the restart
# is worse than finding it out here, where the rollback still works.
venv/bin/python -c "import app.main" 2>/dev/null \
  || die "the app does not import with the installed dependency versions.
    Check:  cd $APP_DIR && venv/bin/python -c 'import app.main'"
ok "app imports cleanly"

# ── 5. Migrate ──────────────────────────────────────────────────────────────
# The step that must not be skipped. Safe to re-run: a no-op when already current.
say "Applying database migrations"

# alembic/env.py does `from app.models import Base`, and the CLI does not put the
# project root on sys.path. Running it from a shell therefore fails with
# ModuleNotFoundError even though the app itself imports fine under uvicorn.
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"

# **The database predates Alembic**, so it has no alembic_version table: this app was
# first deployed when app.main's create_all() built the schema directly. `upgrade head`
# then starts from the very first revision and dies on "table churn_reports already
# exists" — verified by replaying it against a copy of this exact database.
#
# The fix is to STAMP the revision whose schema production already matches, so Alembic
# skips it and applies only what is genuinely new. `products.use_by` is the marker: it
# is what 68e373db239a adds, and it is present, so that revision is the true baseline.
#
# A stamp writes one row and touches no table, so it cannot lose data. Getting the
# baseline WRONG could, which is why it is detected rather than assumed.
# The baseline is detected by INSPECTING THE SCHEMA, never by assuming. Two independent
# reasons the recorded revision can disagree with reality on this box:
#
#   a) The database predates Alembic — it was first built by app.main's create_all(), so
#      there is no alembic_version row at all.
#   b) **create_all() ran again on the NEW code during a restart.** This is the one that
#      actually bit, twice. The lifespan hook calls create_all() on every boot; when the
#      service restarts while the new models are on disk, SQLAlchemy creates every
#      missing table at its FINAL shape. The stamp then still points at an older revision,
#      so `upgrade head` tries to CREATE tables that already exist and dies.
#
# So: compare the live columns against what each revision is known to add, newest first,
# and stamp the newest revision the schema already satisfies. A stamp writes one row and
# emits no DDL, so it cannot lose data — but stamping too NEW would skip a real migration,
# which is why each marker below is a column or table that revision specifically adds.
#
# ⚠ **EVERY NEW MIGRATION MUST ADD A BRANCH HERE, NEWEST FIRST.** This list going stale
# is not a harmless omission — it caused a failed deploy. After 7c1a4e9b2d38 shipped, the
# newest branch was still `users in tables -> 394fc6f28429`, so the detector looked at a
# database already at 7c1a4e9b2d38, decided it was at 394fc6f28429, and stamped it
# BACKWARDS. `upgrade head` then re-ran a migration whose columns already existed and
# died on "duplicate column name". The rollback worked and no data was lost, but the
# deploy failed for a reason that had nothing to do with the code being deployed.
BASELINE="$(venv/bin/python - <<'PY'
import sqlite3

con = sqlite3.connect("tracker.db")
tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}


def cols(table):
    return {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}


if not tables:
    print("")                                       # empty: migrate from scratch
elif "ads_snapshot" in tables:
    print("b8e3f1a67c94")                           # head: portfolio ACOS + settings
elif "economics_snapshot" in tables:
    print("a7c4e91b58d2")                           # portfolio economics
elif "order_packed_state" in tables:
    print("f6b2d4907ae3")                           # per-order packed tick
elif "product_raw_stock" in tables:
    print("e5a1b83c26df")                           # raw stock for purchasing
elif "order_packed_entries" in tables:
    print("d4f9a2c68b31")                           # warehouse packed counts
elif "amazon_orders" in tables:
    print("c3d8e5f21a47")                           # amazon order cache
elif "shipment_packing_days" in tables and "carried_from_plan_id" in cols("shipment_packing_days"):
    print("b2f7c1a94e05")                           # close plans, carry days
elif "product_prices" in tables:
    print("9e4b1c7a2f56")                           # product_prices table
elif "shipment_packing_days" in tables and "inbound_plan_id" in cols("shipment_packing_days"):
    print("7c1a4e9b2d38")                           # Amazon shipment on a day
elif "users" in tables:
    print("394fc6f28429")                           # users + per-area permissions
elif "shipment_packing_entries" in tables and "cartons" not in cols("shipment_packing_entries"):
    print("0f85fa400957")                           # per-entry cartons dropped
elif "shipment_plan_items" in tables and "brand_rank" in cols("shipment_plan_items"):
    print("e886574dd5f5")                           # sort priority, drafts, exclusion
elif "shipment_plans" in tables:
    print("469bf49dd801")                           # shipment tables exist
elif "use_by" in cols("products"):
    print("68e373db239a")                           # initial + use_by
else:
    print("da6e9b47821c")                           # initial only
PY
)"

CURRENT_REV="$(venv/bin/alembic current 2>/dev/null | grep -oE '^[0-9a-f]{12}' | head -1 || true)"
if [ -z "$BASELINE" ]; then
  ok "empty database; migrating from scratch"
elif [ "$CURRENT_REV" = "$BASELINE" ]; then
  ok "recorded revision ($CURRENT_REV) already matches the schema"
else
  if [ -z "$CURRENT_REV" ]; then
    warn "no alembic_version row — this database predates Alembic"
  else
    warn "recorded revision is $CURRENT_REV but the schema is actually at $BASELINE"
    warn "  (create_all() on a restart builds missing tables at their final shape,"
    warn "   which leaves the stamp behind the reality)"
  fi
  printf '    detected baseline: %s\n' "$BASELINE"
  RESTORE_NEEDED=1
  venv/bin/alembic stamp "$BASELINE" || die "could not stamp the baseline revision"
  ok "stamped at $BASELINE — only genuinely-new migrations will run"
fi

BEFORE_REV="$(venv/bin/alembic current 2>/dev/null | tail -1 || echo 'unknown')"
printf '    before: %s\n' "$BEFORE_REV"
RESTORE_NEEDED=1
venv/bin/alembic upgrade head || die "alembic upgrade failed — the schema may be
    partially migrated. The backup is at $BACKUP_FILE"
printf '    after:  %s\n' "$(venv/bin/alembic current 2>/dev/null | tail -1)"
ok "schema is at head"

# Prove the new tables exist rather than trusting the exit code.
venv/bin/python - <<'PY' || die "the migration reported success but the tables are missing"
import sqlite3, sys
con = sqlite3.connect("tracker.db")
have = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
need = {"shipment_plans", "shipment_plan_items", "shipment_packing_days",
        "shipment_packing_entries", "product_categories", "users",
        "amazon_orders", "order_packed_entries", "product_raw_stock",
        "order_packed_state", "economics_snapshot", "product_decision",
        "ads_snapshot", "portfolio_settings"}
missing = sorted(need - have)
if missing:
    print("    missing tables:", missing)
    sys.exit(1)
print("    all shipment and user tables present")
PY

# ── 6. Restart and verify ───────────────────────────────────────────────────
say "Restarting $SERVICE"
sudo systemctl restart "$SERVICE" || die "systemctl restart failed"

# Poll rather than sleep-then-check: uvicorn takes a moment, and a fixed sleep is
# either too short (false failure) or wastes time on every deploy.
printf '    waiting for the app'
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
    printf '\n'
    ok "health check passed"
    break
  fi
  printf '.'
  sleep 1
done
curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1 || die "the app did not come back.
    Logs:  sudo journalctl -u $SERVICE -n 60 --no-pager"

# Verify the NEW features are actually being served. A green health check only proves
# uvicorn started — it would pass just as happily on the old code.
say "Verifying the new build is live"
check_route() {
  local path="$1" expect="$2" label="$3"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:8000$path" || echo 000)"
  if [ "$code" = "$expect" ]; then
    ok "$label ($path -> $code)"
  else
    warn "$label expected $expect, got $code ($path)"
    return 1
  fi
}
FAILED=0
check_route /login 200 "login page"        || FAILED=1
check_route /static/theme.css 200 "light theme stylesheet" || FAILED=1
check_route /ops-page 303 "packing screen (redirects when signed out)" || FAILED=1
check_route /users-page 303 "users panel (redirects when signed out)"  || FAILED=1
[ "$FAILED" -eq 0 ] || die "the app is running but is not serving the new build"

# ── 7. Configuration the new features need ──────────────────────────────────
# Not fatal — the app runs fine without these — but silent absence is what makes a
# feature look broken rather than unconfigured.
say "Checking configuration"
if grep -q '^OPS_PASSWORD=..*' .env 2>/dev/null; then
  ok "OPS_PASSWORD is set (the shared packing login works)"
else
  warn "OPS_PASSWORD is not set in .env."
  warn "  The warehouse cannot use the shared packing login until it is. Either add it:"
  warn "      echo 'OPS_PASSWORD=choose-something' >> $APP_DIR/.env && sudo systemctl restart $SERVICE"
  warn "  ...or create a named Packer account from the Users screen, which is better."
fi

trap - EXIT
cat <<EOF

$(printf '\033[0;32m')✓ Deployed successfully$(printf '\033[0m')

    Version:  $(git rev-parse --short HEAD)
    Backup:   $BACKUP_FILE
    Rollback: git -C $APP_DIR checkout $PREVIOUS_REF && sudo systemctl restart $SERVICE

  Next, in the browser:

    1. Sign in with the shared owner password (APP_PASSWORD from .env).
    2. Open Users and create a real login for yourself, ticked as Administrator.
    3. Sign out, sign in with THAT account, and check it works.
    4. Only then clear APP_PASSWORD in $APP_DIR/.env and restart — the panel warns
       while it is still live, because until then anyone who knows it is a full admin.
       Keep it until step 3 passes: it is how you get back in if something is wrong.

EOF

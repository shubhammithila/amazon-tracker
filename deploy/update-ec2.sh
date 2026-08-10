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

# Uncommitted changes on the server usually mean somebody edited a file in place. The
# checkout below would overwrite them without saying so.
if ! git diff --quiet || ! git diff --cached --quiet; then
  warn "There are uncommitted changes in $APP_DIR:"
  git status --short | sed 's/^/      /'
  read -r -p "    Discard them and continue? [y/N] " reply
  [ "$reply" = "y" ] || [ "$reply" = "Y" ] || die "Stopped. Nothing was changed."
fi

# ── 1. Back up the database FIRST ───────────────────────────────────────────
say "Backing up the database"
mkdir -p "$BACKUP_DIR"
if [ -f tracker.db ]; then
  # sqlite3 .backup rather than cp when available: cp of a live database can capture a
  # torn write if the app happens to be mid-transaction. .backup takes a consistent
  # snapshot of a database that is in use.
  if command -v sqlite3 >/dev/null; then
    sqlite3 tracker.db ".backup '$BACKUP_FILE'" || die "sqlite3 backup failed"
  else
    cp tracker.db "$BACKUP_FILE" || die "could not copy tracker.db"
    warn "sqlite3 not installed; used cp (install with: sudo apt install -y sqlite3)"
  fi
  ok "saved $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"

  # Verify the backup is readable BEFORE relying on it. An unreadable backup is worse
  # than no backup, because it is trusted.
  if command -v sqlite3 >/dev/null; then
    sqlite3 "$BACKUP_FILE" "pragma integrity_check;" | grep -q '^ok$' \
      || die "the backup failed its integrity check — stopping. Nothing was changed."
    ok "backup passes integrity_check"
    printf '    tables: '
    sqlite3 "$BACKUP_FILE" "select count(*) from sqlite_master where type='table';"
  fi
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

# ── 4. Dependencies ─────────────────────────────────────────────────────────
say "Installing dependencies"
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements.txt || die "pip install failed"
ok "dependencies up to date"

# ── 5. Migrate ──────────────────────────────────────────────────────────────
# The step that must not be skipped. Safe to re-run: a no-op when already current.
say "Applying database migrations"
BEFORE_REV="$(venv/bin/alembic current 2>/dev/null | tail -1 || echo 'unknown')"
printf '    before: %s\n' "$BEFORE_REV"
RESTORE_NEEDED=1
venv/bin/alembic upgrade head || die "alembic upgrade failed — the schema may be
    partially migrated. The backup is at $BACKUP_FILE"
printf '    after:  %s\n' "$(venv/bin/alembic current 2>/dev/null | tail -1)"
ok "schema is at head"

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

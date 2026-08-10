"""Password hashing and credential generation. No third-party dependency.

**Why not bcrypt or passlib.** This deploys to a t2.micro by unpacking a tarball and
restarting a systemd unit. Every added wheel is one more thing that can fail to build
on the box at the moment the owner needs the app back. ``hashlib.scrypt`` is in the
standard library, is memory-hard (which is the property bcrypt is chosen for), and
ships with the Python that is already running. If this ever grows past a handful of
users, passlib is a drop-in replacement behind ``hash_password``/``verify_password``.

**Why the salt and parameters live in the stored string.** The format is
``scrypt$n$r$p$salt_hex$hash_hex``. Cost parameters change as hardware does, and a
hash that does not record the parameters it was made with cannot be verified after you
raise them — every user is locked out at once. Recording them means old hashes keep
verifying and only new ones get the stronger settings.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

# ─── Hashing ─────────────────────────────────────────────────────────────────

#: scrypt parameters. n=2**14 with r=8 is ~16 MB and a few tens of milliseconds on a
#: t2.micro — slow enough to make offline guessing expensive, fast enough that a login
#: does not feel broken. Deliberately not higher: this box has 1 GB of RAM and also
#: runs the scraper, and an OOM at login is worse than a slightly cheaper hash.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_PREFIX = "scrypt"


def hash_password(password: str) -> str:
    """A password as it is stored. Never reversible, and never logged.

    A fresh random salt per call, so two users choosing the same password get
    different hashes and a stolen database cannot be scanned for repeats.
    """
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        [_PREFIX, str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P), salt.hex(), digest.hex()]
    )


def verify_password(password: str, stored: str | None) -> bool:
    """Does this password match the stored hash?

    Returns False rather than raising on anything malformed — a truncated or
    hand-edited hash column must fail the login, not 500 the login page for everyone.

    ``hmac.compare_digest`` rather than ``==`` because a plain comparison returns early
    on the first differing byte, and that timing difference is measurable over a
    network for a long enough hash.
    """
    if not password or not stored:
        return False
    try:
        prefix, n, r, p, salt_hex, hash_hex = stored.split("$")
        if prefix != _PREFIX:
            return False
        expected = bytes.fromhex(hash_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


# ─── Generating credentials ──────────────────────────────────────────────────

#: Ambiguous characters removed on purpose. These get written on paper and read aloud
#: across a warehouse: 0/O, 1/l/I and 5/S are where that goes wrong, and a failed login
#: gives no hint which character was misread.
#:
#: Note 5 is excluded as well as 0 and 1 — it is confusable with S in a lot of
#: handwriting, which is the medium these actually travel on. Found by a test that
#: checked the property rather than the alphabet.
_UNAMBIGUOUS = "abcdefghjkmnpqrtuvwxyz" "ACDEFGHJKLMNPQRTUVWXYZ" "234679"

#: The digits appended to a generated password. Same exclusions, same reason.
_SAFE_DIGITS = "234679"

#: Word-ish syllables, so a generated password can be dictated. "vaso-tuki-49" is a
#: different thing to read out than "x7$Kq9!m". Length carries the entropy instead of
#: character-class gymnastics, which is also what NIST recommends.
_CONSONANTS = "bdfghjkmnprstvz"
_VOWELS = "aeiou"


def generate_password(syllables: int = 3) -> str:
    """A password that survives being written down and read aloud.

    Three CV-CV syllable pairs plus two digits: ~40 bits from the syllables and ~6
    from the digits. That is weak against an offline attack on a leaked hash and
    entirely adequate against online guessing at a login form with no automation —
    which is the actual threat model for an app on one EC2 box behind a password.

    The owner can always type something stronger; this is the value the panel offers so
    that "make a login for the new packer" is one click rather than an invention.
    """
    parts = []
    for _ in range(max(1, syllables)):
        parts.append(
            secrets.choice(_CONSONANTS) + secrets.choice(_VOWELS)
            + secrets.choice(_CONSONANTS) + secrets.choice(_VOWELS)
        )
    return "-".join(parts) + "-" + "".join(secrets.choice(_SAFE_DIGITS) for _ in range(2))


def generate_token(length: int = 10) -> str:
    """An opaque random string, for a username suffix on collision."""
    return "".join(secrets.choice(_UNAMBIGUOUS) for _ in range(max(4, length)))


# ─── Usernames ───────────────────────────────────────────────────────────────

#: Deliberately narrow. A username reaches a URL, a log line and a shell command
#: eventually, so anything that needs quoting is refused rather than escaped.
USERNAME_MIN = 3
USERNAME_MAX = 32
_USERNAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


def normalise_username(raw: str) -> str:
    """Lowercased and trimmed. Case-insensitive so `Ravi` and `ravi` are one user.

    Two people who believe they have different accounts and share one is a confusing
    class of bug, and worse when one of them has fewer permissions than the other.
    """
    return str(raw or "").strip().lower()


def username_error(raw: str) -> str | None:
    """Why this username is unacceptable, or None. Messages are shown verbatim."""
    name = normalise_username(raw)
    if not name:
        return "Username is required."
    if len(name) < USERNAME_MIN:
        return f"Username must be at least {USERNAME_MIN} characters."
    if len(name) > USERNAME_MAX:
        return f"Username must be at most {USERNAME_MAX} characters."
    bad = sorted(set(name) - _USERNAME_OK)
    if bad:
        return (
            "Username may only use letters, numbers, dot, dash and underscore. "
            f"Not allowed: {' '.join(bad)}"
        )
    if not name[0].isalnum():
        return "Username must start with a letter or number."
    return None


def suggest_username(full_name: str, taken: set[str] | None = None) -> str:
    """A username from a person's name: "Ravi Kumar" -> "ravi.kumar".

    Falls back to a random suffix on collision rather than incrementing, because
    ``ravi.kumar2`` invites the question "who is ravi.kumar1 and do they still work
    here" — and the answer is usually that nobody remembers.
    """
    taken = {normalise_username(t) for t in (taken or set())}
    cleaned = "".join(
        c if c in _USERNAME_OK else "." for c in normalise_username(full_name)
    )
    # Collapse runs of separators and trim them from the ends.
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    base = cleaned.strip("._-")[:USERNAME_MAX] or "user"
    if len(base) < USERNAME_MIN:
        base = (base + "user")[:USERNAME_MAX]

    if base not in taken:
        return base
    for _ in range(50):
        suffix = generate_token(4)
        candidate = f"{base[:USERNAME_MAX - 5]}.{suffix}"
        if candidate not in taken:
            return candidate
    # Fifty collisions against a 23-character alphabet is not chance; return something
    # unique rather than looping forever.
    return generate_token(USERNAME_MAX - 1)

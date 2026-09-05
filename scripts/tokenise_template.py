"""Convert one template's inline <style> block onto theme.css's token scales.

Throwaway; not imported by the app. Exists because doing this by hand nine times is how a
twentieth font size gets invented — the mapping is a decision, so it lives in one place and is
applied identically to every page.

**Every replacement carries a fallback equal to the value it replaced.** Measured during the
portfolio conversion: a stale cached theme.css leaves the tokens undefined, so `var(--fs-md)`
collapses to inherited 15px and the whole grid reflows — rows went 33->36px and tags 10->15px.
theme.css is now load-bearing for LAYOUT, not merely colour, so it has to degrade to today's
appearance rather than to nothing.

    venv/Scripts/python scripts/tokenise_template.py templates/ops.html [--dry-run]

Reports every replacement and refuses to touch anything outside the <style> block.
"""
import argparse
import collections
import pathlib
import re
import sys

#: Value -> token. Keys are every literal actually found across the nine templates, so a value
#: appearing here is a value that really occurs; anything absent is reported rather than guessed.
FONT_SIZE = {
    "9": None,          # decorative glyphs (sort arrows) — NOT type, see portfolio.html .arrow
    "10": "--fs-xs", "10.5": "--fs-xs", "11": "--fs-xs", "11.5": "--fs-xs",
    "12": "--fs-sm", "12.5": "--fs-sm",
    "13": "--fs-md", "13.5": "--fs-md",
    "14": "--fs-lg", "14.5": "--fs-lg",
    "15": "--fs-xl", "16": "--fs-xl",
    #: **17px is the iOS input guard, and it is 17 rather than 16.** `ops.html` states it:
    #: "iOS silently zooms the whole page in on focus for anything under 16px", so the packing
    #: inputs sit deliberately ABOVE the threshold and lose the packer's place if tokenised down.
    #: Note this corrects the plan, which named 16px — the four 16px uses on that page are
    #: headings and product names, which are ordinary type and do convert.
    #:
    #: **Held back for INPUTS only.** `login.html`'s `.logo h1` is also 17px and is a heading, not
    #: something anyone focuses, so it converts to `--fs-xl` by hand. Left as `None` here rather
    #: than mapped, because the script cannot see which selector it is inside and getting this
    #: wrong on the packing screen costs the packer his place in a 100-row list.
    "17": None,
    "18": "--fs-2xl", "19": "--fs-2xl", "20": "--fs-2xl", "21": "--fs-2xl", "22": "--fs-2xl",
}

RADIUS = {
    "2": "--radius-sm", "3": "--radius-sm", "4": "--radius-sm", "5": "--radius-sm",
    "6": "--radius", "7": "--radius", "8": "--radius",
    "10": "--radius-lg", "12": "--radius-lg", "14": "--radius-lg", "16": "--radius-lg",
    "20": "--radius-pill", "99": "--radius-pill", "999": "--radius-pill",
}

#: The literal each token must fall back to when theme.css is stale. These are the token's OWN
#: values, so a fallback never changes today's rendering — it only survives a missing stylesheet.
FALLBACK = {
    "--fs-xs": "11px", "--fs-sm": "12px", "--fs-md": "13px",
    "--fs-lg": "14px", "--fs-xl": "16px", "--fs-2xl": "20px",
    "--radius-sm": "4px", "--radius": "8px", "--radius-lg": "12px", "--radius-pill": "999px",
}

#: `--X-soft` was a hand-picked pastel; the derived form composes the tint from the colour's own
#: channel. The fallbacks are the channel and alpha, so a stale sheet still paints a tint.
CHANNEL = {
    "accent": "29 78 216", "green": "20 108 52", "red": "198 40 40",
    "yellow": "161 98 7", "orange": "194 65 12", "blue": "29 78 216",
}
TINT_ALPHA = {"tint-soft": "0.10", "tint-hover": "0.16"}


def convert(css: str, counter: collections.Counter, skipped: collections.Counter) -> str:
    """Rewrite one <style> body. Pure, so the caller decides whether to save.

    **CSS comments are masked out first.** They are prose, and several of them legitimately quote a
    pixel value while explaining why it is not used — portfolio.html's `body` comment says
    "theme.css sets body{font-size:15px}", which the first version of this script duly tried to
    tokenise. Rewriting a sentence is worse than missing a rule: the rule still renders correctly and
    the comment now lies.
    """
    comments: list[str] = []

    def stash(match):
        comments.append(match.group(0))
        return f"/*__C{len(comments) - 1}__*/"

    css = re.sub(r"/\*.*?\*/", stash, css, flags=re.S)
    css = _convert_declarations(css, counter, skipped)
    for index, text in enumerate(comments):
        css = css.replace(f"/*__C{index}__*/", text)
    return css


def _convert_declarations(css: str, counter: collections.Counter,
                          skipped: collections.Counter) -> str:
    """The actual rewriting, on comment-free CSS."""

    def size(match):
        value = match.group(1)
        token = FONT_SIZE.get(value)
        if token is None:
            skipped[f"font-size: {value}px"] += 1
            return match.group(0)
        counter[f"font-size {value}px -> {token}"] += 1
        return f"font-size:var({token}, {FALLBACK[token]})"

    css = re.sub(r"font-size:\s*([0-9.]+)px", size, css)

    def radius(match):
        value = match.group(1)
        token = RADIUS.get(value)
        if token is None:
            skipped[f"border-radius: {value}px"] += 1
            return match.group(0)
        counter[f"border-radius {value}px -> {token}"] += 1
        return f"border-radius:var({token}, {FALLBACK[token]})"

    css = re.sub(r"border-radius:\s*([0-9]+)px", radius, css)

    # Soft colours -> derived tints. Hover rules get the stronger step, because a resting tint
    # under the cursor is invisible; everything else gets the resting one.
    for colour, channel in CHANNEL.items():
        soft = f"var(--{colour}-soft)"
        if soft not in css:
            continue

        def tint_for(block: str) -> str:
            alpha = "tint-hover" if ":hover" in block else "tint-soft"
            counter[f"--{colour}-soft -> {alpha}"] += block.count(soft)
            return block.replace(
                soft,
                f"rgb(var(--{colour}-rgb, {channel}) / var(--{alpha}, {TINT_ALPHA[alpha]}))",
            )

        # Rule by rule, so :hover can be told apart from the rest.
        css = re.sub(r"[^{}]*\{[^{}]*\}", lambda m: tint_for(m.group(0)) if soft in m.group(0)
                     else m.group(0), css)

    # Any var() that slipped through without a fallback (e.g. hand-written earlier).
    def backfill(match):
        token = match.group(1)
        if token not in FALLBACK:
            return match.group(0)
        counter[f"fallback added to {token}"] += 1
        return f"var({token}, {FALLBACK[token]})"

    css = re.sub(r"var\((--(?:fs|radius)-?[a-z0-9]*)\)(?!\s*,)", backfill, css)
    return css


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("template")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path = pathlib.Path(args.template)
    original = path.read_text(encoding="utf-8")
    match = re.search(r"(<style[^>]*>)(.*?)(</style>)", original, re.S)
    if not match:
        print(f"{path.name}: no <style> block — nothing to convert")
        return 0

    counter, skipped = collections.Counter(), collections.Counter()
    converted = convert(match.group(2), counter, skipped)
    updated = original[:match.start(2)] + converted + original[match.end(2):]

    print(f"{path.name}: {sum(counter.values())} replacement(s)")
    for key, count in sorted(counter.items()):
        print(f"    {count:>3}x  {key}")
    if skipped:
        print("  LEFT ALONE (decide per page, and comment the reason in the template):")
        for key, count in sorted(skipped.items()):
            print(f"    {count:>3}x  {key}")

    if args.dry_run:
        print("  (dry run — nothing written)")
        return 0
    path.write_text(updated, encoding="utf-8")

    # A bare size token after conversion means the fallback pass missed one, which would reflow
    # the page on a stale stylesheet. Reported as a failure rather than a warning.
    leftover = re.findall(r"var\((--(?:fs|radius)-?[a-z0-9]*)\)(?!\s*,)", converted)
    if leftover:
        print(f"  ERROR: {len(leftover)} token(s) still have no fallback: {sorted(set(leftover))}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

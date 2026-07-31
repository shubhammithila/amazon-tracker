"""templates/invoice.html's side of the shipment handoff.

Same no-browser approach as tests/test_shipment_admin_ui.py: nothing here renders
JavaScript, so the two halves of a handoff can disagree about the storage key or
a field name and both pages will still load perfectly.

That is not hypothetical for this pair. The shipment page writes a payload and
navigates away; the invoice page reads it on a fresh document. There is no shared
runtime and no error if the key is wrong — the invoice page simply shows its
empty upload screen, which looks exactly like a normal visit. The owner would
conclude the Invoice button "does nothing".

The GST-relevant checks are the other reason this file exists. An invoice built
from packing has no Amazon shipment id and no FC, and both of those feed a tax
document:

* ``/invoice/save`` refuses a blank ``shipment_id``, so if the field is readonly
  the whole bridge is a dead end with no visible cause.
* The recipient address used to be built as ``wh.city + ", " + wh.state``, which
  with no warehouse renders the literal text "undefined, undefined undefined"
  onto the invoice. That reads like a formatting slip, not a bug, and would
  plausibly be saved.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INVOICE = REPO_ROOT / "templates" / "invoice.html"
SHIPMENT = REPO_ROOT / "templates" / "shipment.html"

pytestmark = pytest.mark.regression


@pytest.fixture(scope="module")
def source() -> str:
    return INVOICE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def shipment_source() -> str:
    return SHIPMENT.read_text(encoding="utf-8")


def _without_comments(source: str) -> str:
    """Blank the comment regions, keeping line numbers. See test_shipment_admin_ui."""
    def blank(match: re.Match) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    for pattern in (r"\{#.*?#\}", r"<!--.*?-->", r"/\*.*?\*/"):
        source = re.sub(pattern, blank, source, flags=re.S)
    return source


# ─── The two halves must agree ───────────────────────────────────────────────

def test_both_pages_use_the_same_handoff_key(source, shipment_source):
    """The failure mode is silent on both sides.

    A mismatched key means the invoice page finds nothing, shows its normal empty
    upload screen, and reports no error at all. The Invoice button looks like it
    does nothing.
    """
    def keys(text):
        return set(re.findall(r'["\'](shipment[A-Za-z]*[Pp]ayload)["\']', text))

    invoice_keys = keys(source)
    shipment_keys = keys(shipment_source)
    assert invoice_keys, "templates/invoice.html reads no handoff key"
    assert shipment_keys, "templates/shipment.html writes no handoff key"
    assert invoice_keys & shipment_keys, (
        f"the pages disagree about the sessionStorage key: invoice reads "
        f"{sorted(invoice_keys)}, shipment writes {sorted(shipment_keys)} — the "
        "handoff would silently do nothing"
    )


def test_the_invoice_page_reads_the_handoff(source):
    body = _without_comments(source)
    assert "sessionStorage.getItem" in body, "the invoice page never looks for a handoff"
    assert "populateItems(" in body, "the handoff does not populate the line items"
    assert "recalculate(" in body, "totals are never calculated for a handoff invoice"


def test_the_handoff_is_consumed_once(source):
    """Removed on read, so a reload cannot re-open packing already invoiced.

    Without this, refreshing the invoice page after saving would silently present
    the same verified days again — and the second invoice would be blocked by the
    server, but only after the owner had filled the form in.
    """
    body = _without_comments(source)
    assert "sessionStorage.removeItem" in body, (
        "the handoff is never cleared, so a reload re-opens days that may already "
        "be invoiced"
    )


def test_the_handoff_reuses_the_tsv_rendering_path(source):
    """One way to build a GST document, not two.

    A separate render for handoff invoices would be a second place for the
    document to be built slightly differently, and only the TSV one is covered by
    the existing invoice tests.
    """
    body = _without_comments(source)
    assert len(re.findall(r"function populateItems", body)) == 1, (
        "populateItems was forked — a GST document must not have two rendering paths"
    )


# ─── Fields that decide what appears on a tax document ───────────────────────

@pytest.mark.parametrize("field", ["f-shipment-id", "f-place-supply"])
def test_the_amazon_fields_can_be_typed_into(source, field):
    """They must not be readonly, or the bridge is a dead end.

    Amazon issues the shipment id and assigns the FC only after the shipment
    exists, so a handoff invoice arrives with neither. /invoice/save refuses a
    blank shipment_id — with these locked, the owner reaches the last step and
    simply cannot proceed, with nothing on screen explaining why.
    """
    match = re.search(rf'<input[^>]*id="{field}"[^>]*>', source)
    assert match, f"{field} is missing from the form"
    assert "readonly" not in match.group(0), (
        f"{field} is readonly, so an invoice built from packing can never be "
        "saved — /invoice/save requires a shipment id"
    )


def test_the_recipient_address_cannot_render_as_undefined(source):
    """The literal string "undefined, undefined undefined" on a GST invoice.

    `wh.city + ", " + wh.state + " " + wh.pincode` produces exactly that when the
    payload has no warehouse, which is every handoff invoice. It looks like a
    formatting slip rather than a bug, so it would be saved.
    """
    body = _without_comments(source)
    assert 'wh.city + ", " + wh.state' not in body, (
        "the recipient address is still concatenated from possibly-undefined "
        'fields — a handoff invoice would read "undefined, undefined undefined"'
    )
    assert re.search(r"metadata\.warehouse\s*\|\|\s*\{\}", body), (
        "metadata.warehouse is not defaulted, so a handoff payload with no "
        "warehouse would throw on property access"
    )


def test_the_cartons_prefill_the_boxes_field(source):
    """Requirement 7's payoff: the daily carton count is not re-counted by hand."""
    body = _without_comments(source)
    assert re.search(r'data\.boxes', body), (
        "the handoff's carton total never reaches the Boxes field, so entering "
        "cartons daily achieved nothing"
    )
    assert "f-boxes" in body


# ─── Closing the loop back to the shipment ───────────────────────────────────

def test_a_saved_invoice_is_attached_back_to_the_packing_days(source):
    """Otherwise nothing records that those boxes are spoken for.

    That record is exactly what the double-invoice guard checks, so without this
    call the same days could be invoiced again.
    """
    body = _without_comments(source)
    assert "/shipment/attach-invoice" in body, (
        "the invoice is never attached to the packing days — the double-invoice "
        "guard has nothing to fire on"
    )
    assert "invoice_id" in body, "the attach call sends no invoice id"


def test_a_failed_attach_is_surfaced_not_swallowed(source):
    """The one genuinely unprotected window in the whole flow.

    If this call fails the invoice exists while the days still read `verified`, so
    the app believes they are un-invoiced. It is recoverable, but only if somebody
    knows — a console.error would leave the owner with no idea the two records had
    diverged.
    """
    body = _without_comments(source)
    attach = body.find("/shipment/attach-invoice")
    assert attach > 0
    window = body[attach:attach + 1600]
    assert "alert(" in window, (
        "a failed attach is not surfaced to the owner, so the invoice and the "
        "packing record would silently disagree about what has been invoiced"
    )


def test_the_attach_is_not_wired_into_invoice_save(source):
    """POST /invoice/save must stay untouched — 26 tests guard the GST sequence.

    The attach is a separate request on purpose. A bug in this bookkeeping rolling
    back a committed invoice would burn a number out of a legally-sequential
    series, which cannot be undone; the reverse failure can be retried.
    """
    body = _without_comments(source)
    save = body.find('fetch("/invoice/save"')
    attach = body.find('fetch("/shipment/attach-invoice"')
    assert save > 0 and attach > save, (
        "the attach does not follow the save — the invoice must be committed "
        "first, since only its failure is recoverable"
    )

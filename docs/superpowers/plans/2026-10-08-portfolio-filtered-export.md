# Portfolio — filtered Excel + PDF download that matches the screen

> Execute INLINE (superpowers:executing-plans; this user forbids subagents).

**Goal:** Excel and PDF buttons on the Portfolio tab that download exactly the rows on screen
(category card, group tab, custom filters, search, Products/SKUs, window, inactive toggle),
with the screen's columns in the screen's order, numbers stored as numbers, and no commentary.

## Asked for
- *"say I have just selected sattu… download only sattu"*; *"searched or put a filter… download
  just that too"*
- *"text which are numbers stored as numbers or %"*; *"same format as what we see on the screen"*
- *"give both pdf and excel option"*; *"no additional commentary in the excel. just data."*
- *"properly formatted… columns and rows… data properly displayed for best view"*

## Measured on the file the owner downloaded (portfolio-2026-10-07.xlsx)
| Defect | Example |
|---|---|
| percentages are TEXT | `'30.0%'`, `'36.7%'` (type `str`, format General) |
| weights are TEXT | `'1,453.0 kg'` |
| rating is TEXT | `'4.0 (388)'` |
| row 1 is commentary | a ~600-char sentence: window, caveats, exclusions |
| Brand column holds prose on size rows | `'merchant 9,043 + fba 254,191'` |
| a 52-wide prose column | `Why` |
| no number formats, no filter buttons | every cell `General`, `auto_filter None` |
| column order differs from the screen | Verdict, Net %, TACOS, ACOS, Sales… |

## Decisions
1. **The browser chooses ROWS, the server computes NUMBERS.** `POST /portfolio/export` receives the
   ids `visible()` returns (on-screen order), the expanded ids (`open`), the visible column ids, the
   view, the window and `include_inactive`. The server rebuilds `_dashboard` and keeps only those
   ids. Every filter the screen has is honoured with no second copy of the filter logic, and no
   figure travels from the client. Unknown ids are ignored; unknown column ids are dropped.
2. **Columns:** four lead columns — Product · Brand · Size · ASIN — then the visible columns in the
   screen's order with the screen's labels (`columns.COLUMNS`). Rating splits into **Rating** and
   **Reviews** so both are numbers. The "Why" prose and the merchant/FBA sentence are dropped.
3. **Types and formats** — stored as numbers, displayed like the screen:

   | Column | Stored | Excel format |
   |---|---|---|
   | Sales, Ad spend | number | `₹` + Indian lakh grouping, no decimals |
   | Refunds %, Amazon fees %, TACOS, Returns | fraction | `0.0%` |
   | Net % | fraction | `+0.0%;-0.0%;0.0%` |
   | ACOS | fraction, or text `no sales` | `0%` |
   | Units ordered, Net units | integer | Indian grouping |
   | Weight ordered, Net weight | number | Indian grouping, `0.0" kg"` |
   | Rating / Reviews | number / integer | `0.0` / Indian grouping |
   | Verdict, Decision | text | — |

   A missing value is a BLANK cell, never 0 — the screen's "a dash, never a zero".
4. **Excel layout:** Arial 10. Row 1 = totals (as on screen, above the rows), row 2 = headings with
   AutoFilter, row 3+ = data; frozen at E3 so product identity, headings and totals stay visible.
   Products view: sizes (and flavour headings for multi-flavour parents) are OUTLINE-grouped under
   their parent — expanded where expanded on screen, collapsed otherwise. Fitted column widths,
   thin grid, totals bold on a light fill. No title or commentary rows.
5. **Totals = the screen's `computeTotals`**, ported to Python (`export.totals`) and pinned by a test
   that runs the page's own JS on the same rows. Written as values (percentages need rupee sums that
   are not columns), so the Total cell carries a comment saying how they were computed — the xlsx
   skill's rule for figures Excel does not compute.
6. **PDF:** landscape A4, DejaVu Sans Condensed (bundled in `app/fonts/`; the box has no TTF fonts
   and reportlab's base fonts lack ₹), one title line (window + filter), headings repeated each page,
   totals first, expanded sizes indented. Every cell a `Paragraph` (the house rule).
7. **Filename** `portfolio-<start>_<end>[-<filter>].xlsx|pdf`, the filter slugged from the label.
8. **`GET /portfolio/download.xlsx` stays** as the unfiltered export (every row, every column) in the
   NEW format, so it remains a way to get everything. The UI uses the POST.

> **Reversals of earlier decisions, recorded:** the workbook used to carry the pre-COGS caveat and
> the Active-flag exclusion in row 1, and "the Excel always has every column". The owner's
> instruction now is *just data* and *same as the screen*. The filtered export follows the screen;
> the GET keeps every column.

## Tasks
1. **`app/portfolio/export.py`** — `COLUMN_KINDS`, `select_rows`, `totals`, `build_table`,
   `build_xlsx`, `build_pdf`, `filename`. Tests `tests/test_portfolio_export.py`: types and number
   formats per column; blank-not-zero; only the requested ids, in the requested order; outline
   levels and hidden state follow `open`; totals parity with the page's JS `computeTotals`; no
   commentary (row 1 is totals, row 2 headers); PDF renders ₹ and is a valid PDF.
2. **Route** `POST /portfolio/export` + GET rewired; tests through the route.
3. **Template** — Excel and PDF buttons; `exportPayload()` built from `visible()`, `open`,
   `visibleColumns()`; `filterLabel()`; fetch → blob download. Node test of the payload.
4. **Update the 7 dependent tests** to the new format; fix the harness entry; browser check;
   CLAUDE.md; full suite + harnesses; commit, push, deploy, verify a real download on production.

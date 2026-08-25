# Leads Sorter — CRM Portal

A small local web portal that takes an **unorganized leads CSV**, cleans and
standardizes it, shows it in a sortable table, and gives back an **organized CSV**.

Output columns (exact order):

```
Timestamp, Full Name, Date of Birth, Address, Phone Number,
ZIP Code, Homeowner, Autos, Current Insurance, Cars Make and Model
```

**Agent Name and Leads Note columns are always dropped** — they never appear in
the output, even if the input file contains them.

## Easiest way to share: the single-file edition

**[LeadsSorter.html](LeadsSorter.html)** is the whole tool in ONE file. Send it
to anyone (email, USB stick, chat) — they double-click it and it opens in
their browser. No Python, no installs, no server, no internet. All processing
happens inside their browser; the file never leaves their laptop.

It carries the full pipeline: CSV/TSV/XLSX reading (Excel parsed with a
built-in zip reader, no libraries), grid-card parsing, header hunting,
split-name/address stitching, cleaning, dedupe, scoring, and all three
downloads (organized CSV, change report, error log). Needs a browser from
2021 or newer (Chrome, Edge, Firefox) for Excel files; old-format .xls should
be re-saved as .xlsx first.

## Run it

```
pip install -r requirements.txt
python app.py            # just for you:   http://127.0.0.1:5000
python run_server.py     # for the team:   LAN-wide on port 8080 (see SHARING.md)
python clean.py FILE     # no browser:     CLI, writes cleaned CSV + reports
```

Windows: double-click `start_server.bat`. Linux/Mac: `./start_server.sh`.
The LAN server prints the exact URL colleagues should open, falls back to the
next free port automatically, honors an optional shared passphrase, limits
upload size (default 50 MB), and auto-deletes job files after 24 h — all
configured in `server_config.json`. Fully offline: no CDN, no cloud calls.

## Deploy to Render

The app stores each job's files on local disk (`uploads/`, `output/`) and
reads them back on the download links, so it needs a normal long-running
process with a persistent-during-uptime filesystem — Render's free web
service tier works out of the box, no code changes needed. Platforms with a
stateless/serverless Python runtime (e.g. Vercel functions) will NOT work
as-is: uploads and downloads land on different, isolated invocations, so the
download links 404/500.

1. Push this repo to GitHub (already done if you're reading this here).
2. On [Render](https://dashboard.render.com), **New → Blueprint**, point it at
   this repo — it picks up [`render.yaml`](render.yaml) automatically (build:
   `pip install -r requirements.txt`, start: `gunicorn app:app`).
3. Keep it to **one instance / one worker** (already set in `render.yaml`).
   Job files live on that instance's local disk only — scaling to multiple
   instances would send a download request to an instance that never saw the
   upload.

Inputs: CSV, TSV, semicolon/pipe-delimited, XLSX/XLSM/XLS (all sheets).
File type is detected from content, so a mislabeled extension won't crash it;
UTF-8/CP1252/Latin-1 encodings are handled automatically.

Try it with `sample_data/messy_leads.csv` — upload, review the summary +
table, and download the organized CSV, change report, and error log.

## Supported input layouts

1. **Normal tables** — one lead per row, with a header (found even under junk rows).
2. **Headerless tables** — one lead per row, no header; columns are inferred
   from the data content (phones, dates, ZIPs, carriers, vehicles…).
3. **Grid / card sheets** — one lead per COLUMN as labeled cells
   (`Name:`, `DOB:`, `Number:`, `Address:`, `City:`, `Zip Code:`, homeowner
   lines, vehicles, insurance), in blocks separated by banner rows. Each card
   is flattened into one output row; order is blocks top-to-bottom, then
   columns left-to-right.
4. **Linear card text (.txt)** — the same labeled-cell layout as grid sheets,
   but one label per LINE in a plain text file (no columns, no delimiter at
   all), with leads separated by a banner or an underscore rule
   (`____________`). Detected from the raw text before any delimiter
   sniffing runs, so a stray comma in an address line can't derail it.
5. **Labeled-column form exports** — CRM/quoting-tool CSV exports where each
   field is its own row (the label text, e.g. `First Name`, repeated across
   every lead's column) with the value on the row(s) below. Parsed per
   COLUMN as an independent sequence of label→value pairs rather than by any
   shared row index, because these exports commonly drift out of
   row-alignment between columns (leads carry a different number of optional
   sub-records — spouse, additional drivers, violations, vehicles) and a
   single column can stack more than one lead if the block repeats down it.
   Homeowner status is typically **not recoverable** from this layout: the
   option rows (e.g. `Rent` / `Own`) are static template text repeated
   identically for every lead, with no per-lead marker for which one was
   actually selected — left as `NA` rather than guessed. Vehicle
   (Year/Make/Model) and insurance-carrier detection are best-effort.
6. **Verifier/dialer dashboard scrapes** — a CSV export of a lead-verifier
   web app's rendered table, where real lead data is interleaved with UI
   chrome (button labels like `Refresh`, dropdown placeholders like
   `Select Agent`/`Select State`, record UUIDs, footer/nav text). Each
   lead's card is bounded by a `Refresh` button label; the phone number that
   triggered the transfer sits in a short preamble just *before* that
   `Refresh`, not inside the card. An `Agent Info` section right after each
   card repeats `Full Name` for the agent, not the lead, so field extraction
   stops there. Insurance-carrier text often has a trailing duration clause
   (`"porgressive more then 1 year"`) stripped before the normal
   typo-correcting carrier normalizer runs on what's left.

None of these six patterns will cover every possible export forever — a
brand-new lead source can still show up in a layout nothing above
recognizes. When that happens, the portal detects it: if a file parses into
rows but more than half of them are missing both Full Name and Phone
Number, a warning banner appears above the results ("this file's layout
doesn't look fully recognized") instead of silently presenting a table full
of `NA` as if it succeeded.

## What the organizer does

- Maps messy headers to schema columns via aliases + fuzzy matching
  (`DOB` → Date of Birth, `Phone#` → Phone Number, `Carrier` → Current Insurance, …).
- Drops Agent Name / Leads Note / Notes columns entirely.
- Normalizes values:
  - Names → Title Case (fixes `LARRY HENDERSON`; already-mixed-case names kept as-is)
  - Dates → `MM/DD/YYYY`; timestamps keep time as `MM/DD/YYYY HH:MM` when present
  - Phones → `(XXX) XXX-XXXX`; wrong-length numbers kept as digits and flagged/highlighted
  - ZIPs → 5-digit strings, leading zeros preserved, ZIP+4 trimmed
  - Homeowner → exactly `Owner` or `Rented`
  - Carriers → canonical names (`Gieco` → GEICO, `prograssive` → Progressive, `STATE FARM` → State Farm, …)
  - Autos → integer; anything missing → `NA`
- De-duplicates only when BOTH the phone number and the name match (first row
  wins). Two different people sharing a phone are both kept. Rows without a
  phone are never collapsed. Every removed duplicate is listed in the summary.
- Keeps leads in the same order as the uploaded file (a profile can opt into
  sorting via `sort_by` in its schema JSON).

## Refinement layer (rules, scoring, reports)

After organizing, a refinement pass ([refinement.py](refinement.py), configured
by `schemas/refinement.json`) runs automatically:

- **Vertical detection** — column signatures classify the file (insurance /
  real estate / general sales); shown in the summary, default "general".
- **Cross-field rules** (report-only, never mutate data): implausible DOB age,
  DOB vs. explicit Age column, DOB vs. occupation (student/retired),
  invalid/suspicious emails and names, invalid phones.
- **Email typo fixes** — domains checked against a known list; `gmial.com` →
  `gmail.com` auto-applied when similarity ≥ 0.85 (logged), else suggested.
  Emails are auxiliary data — captured and checked but never added to the CSV.
- **Fuzzy dedup** — blocks on phone/email, compares names with nickname
  handling (Jon/John, Bob/Robert); duplicates get a group ID and are flagged
  for merge review, never auto-deleted. (Exact same-name+phone rows are still
  removed, as before.)
- **Lead quality score 0–100** — weighted field completeness minus rule
  penalties; leads under the threshold (70) go to the manual-review bucket
  (amber rows in the table). Scores appear in the UI and error log only —
  the downloaded CSV keeps the standard 10 columns.

Every download comes as three files: the **organized CSV**, a **change report**
(`row, field, original, new, reason` for every modification), and an
**error log** (JSON: scores, violations, dup groups, email fixes, unmapped
headers). Weights, penalties, thresholds, nicknames, verticals, and domains
are all data in `schemas/refinement.json` — no code changes needed to tune.

Aux fields (recognized but kept out of the CSV): Email, Age, Occupation,
Country — defined under `aux_columns` in the schema.

## Tests

```
python -m pytest tests/ -v
```

17 test cases cover header aliasing, name/date/phone/ZIP/homeowner/carrier
normalization, agent/notes dropping, de-duplication, blank/empty files, column
order, sorting, plus an end-to-end portal upload→download round trip. All test
data is fabricated.

## Adding a new portal profile (different sheet / columns / organization)

Everything is driven by JSON configs in `schemas/` — the code never hardcodes
columns. To make a new profile:

1. Copy `schemas/default.json` to e.g. `schemas/mortgage.json`.
2. Edit `columns` (name, order, `type`, `aliases`), `drop_columns`,
   `dedupe_on`, and `sort_by`.
   - Available `type`s: `text`, `name`, `date`, `datetime`, `phone`, `zip`,
     `homeowner`, `int`, `carrier`.
3. Post the form with `schema=mortgage` (change the hidden `schema` input in
   `templates/index.html`, or add a dropdown listing files in `schemas/`).

## Decisions made (per the brief's "decide and document" rule)

- Date format `MM/DD/YYYY`, phone format `(XXX) XXX-XXXX` (US-style, matches the data).
- Row order: same as the uploaded file (no re-sorting by default).
- Unknown insurance carriers are kept (tidied to Title Case) rather than blanked to NA.
- Invalid phones are kept as raw digits (so the lead isn't lost) and flagged in the
  summary + highlighted red in the table.

## Layout

```
app.py                 Flask portal (upload → organize → table → download)
organizer.py           Core cleaning logic (no Flask; unit-testable)
schemas/default.json   Default portal profile (columns, aliases, rules)
templates/index.html   Single-page UI
sample_data/           Fabricated messy sample CSV
tests/                 pytest suite (17 cases)
uploads/, output/      Per-session files (created at runtime)
```

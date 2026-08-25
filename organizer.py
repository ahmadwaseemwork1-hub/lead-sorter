"""Core lead-list organizing logic.

Decoupled from Flask on purpose: everything is driven by a schema config
(see schemas/*.json), so new portal profiles with different columns or
organization methods only need a new JSON file, not new code.
"""

import csv as _csv
import difflib
import io
import json
import os
import re
from datetime import datetime

import pandas as pd

NA = "NA"

_MISSING = {"", "na", "n/a", "none", "null", "-", "--", "nan"}

_PHONE_OK = re.compile(r"\(\d{3}\) \d{3}-\d{4}")

_CANONICAL_CARRIERS = [
    "State Farm", "GEICO", "Progressive", "Allstate",
    "USAA", "Liberty Mutual", "United", "Aetna",
    "Farm Bureau", "Auto-Owners", "Hartford", "National General",
    "Nationwide", "AAA", "Mercury", "Cincinnati", "Direct Auto",
    "Go Auto", "Bristol West", "Root Insurance", "Travelers", "Farmers",
]
_CARRIER_KEYS = {re.sub(r"[^a-z]", "", c.lower()): c for c in _CANONICAL_CARRIERS}
_CARRIER_KEYS.update({
    "gieco": "GEICO",
    "prograssive": "Progressive",
    "progresive": "Progressive",
    "liberty": "Liberty Mutual",
    "statefram": "State Farm",
    "root": "Root Insurance",
    "assurance": "Assurance",
    "alphainsurance": "Alpha Insurance",
    "atlinsurancegroup": "ATL Insurance Group",
})

# carrier spotting inside free text (grid-style files); first match wins
_CARRIER_PATTERNS = [
    (re.compile(r"farm\s*bureau", re.I), "Farm Bureau"),
    (re.compile(r"stat\s*e?\s*farm|sate\s*farm", re.I), "State Farm"),
    (re.compile(r"progr[ae]?s{1,2}ive", re.I), "Progressive"),
    (re.compile(r"g[ei]{1,2}co", re.I), "GEICO"),
    (re.compile(r"\busaa\b", re.I), "USAA"),
    (re.compile(r"allstate", re.I), "Allstate"),
    (re.compile(r"liberty", re.I), "Liberty Mutual"),
    (re.compile(r"hartford", re.I), "Hartford"),
    (re.compile(r"auto\s*(owners?|onwers?)", re.I), "Auto-Owners"),
    (re.compile(r"\baaa\b", re.I), "AAA"),
    (re.compile(r"national\s*gen[ae]ral", re.I), "National General"),
    (re.compile(r"nation\s*wide", re.I), "Nationwide"),
    (re.compile(r"mercury", re.I), "Mercury"),
    (re.compile(r"cincinnati", re.I), "Cincinnati"),
    (re.compile(r"direct\s*auto", re.I), "Direct Auto"),
    (re.compile(r"go\s*auto", re.I), "Go Auto"),
    (re.compile(r"brist[ao]l\s*west", re.I), "Bristol West"),
    (re.compile(r"\broot\b", re.I), "Root Insurance"),
    (re.compile(r"travelers", re.I), "Travelers"),
    (re.compile(r"\bfarmers\b", re.I), "Farmers"),
    (re.compile(r"\bassurance\b", re.I), "Assurance"),
    (re.compile(r"alpha\s*ins\w*", re.I), "Alpha Insurance"),
    (re.compile(r"atl\s*insurance", re.I), "ATL Insurance Group"),
]

_OWNER_WORDS = {"owner", "own", "owns", "owned", "homeowner", "home owner", "yes", "y", "o"}
_RENTED_WORDS = {"rented", "rent", "rents", "renter", "renting", "tenant", "lease", "no", "n", "r"}


def load_schema(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- headers

def _norm_header(header):
    return re.sub(r"[^a-z0-9]+", " ", str(header).lower()).strip()


def _alias_map(schema):
    alias_to_col = {}
    for col in schema["columns"] + schema.get("aux_columns", []):
        alias_to_col[_norm_header(col["name"])] = col["name"]
        for alias in col.get("aliases", []):
            alias_to_col[_norm_header(alias)] = col["name"]
    return alias_to_col


def map_headers(headers, schema):
    """Match input headers to schema columns via aliases, then fuzzy match.

    Returns (mapping, dropped, unmapped, compose) where mapping is
    {original_header: schema_column_name} and compose is
    {schema_column: [source_header_or_None per part]} for fields split
    across several input columns (First+Last Name, Street+City+State).
    """
    alias_to_col = _alias_map(schema)
    drop = {_norm_header(d) for d in schema.get("drop_columns", [])}

    mapping, dropped, unmapped = {}, [], []
    claimed = set()
    for header in headers:
        n = _norm_header(header)
        if n in drop or difflib.get_close_matches(n, list(drop), n=1, cutoff=0.85):
            dropped.append(header)
            continue
        target = alias_to_col.get(n)
        if target is None:
            close = difflib.get_close_matches(n, list(alias_to_col), n=1, cutoff=0.8)
            if close:
                target = alias_to_col[close[0]]
        if target and target not in claimed:
            mapping[header] = target
            claimed.add(target)
        else:
            unmapped.append(header)

    compose = {}
    for target, part_alias_lists in schema.get("compose", {}).items():
        if target in claimed:
            continue
        parts = []
        for alias_list in part_alias_lists:
            wanted = {_norm_header(a) for a in alias_list}
            parts.append(next(
                (h for h in unmapped if _norm_header(h) in wanted), None))
        if sum(p is not None for p in parts) >= 2:
            compose[target] = parts
            claimed.add(target)
            for p in parts:
                if p is not None:
                    unmapped.remove(p)
    return mapping, dropped, unmapped, compose


# ---------------------------------------------------------------- values

def _clean(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def _is_missing(s):
    return s.lower() in _MISSING


def _cap_word(word):
    return "-".join(p[:1].upper() + p[1:].lower() if p else p for p in word.split("-"))


def normalize_text(value):
    s = _clean(value)
    return NA if _is_missing(s) else s


def normalize_name(value):
    s = _clean(value)
    if _is_missing(s):
        return NA
    words = []
    for w in s.split(" "):
        # only re-case words that carry no casing signal (ALL CAPS / all lower)
        words.append(_cap_word(w) if w.isupper() or w.islower() else w)
    return " ".join(words)


def _parse_dt(s):
    try:
        return pd.to_datetime(s)
    except (ValueError, TypeError, OverflowError):
        return None


def normalize_date(value):
    s = _clean(value)
    if _is_missing(s):
        return NA
    dt = _parse_dt(s)
    return dt.strftime("%m/%d/%Y") if dt is not None else NA


def normalize_timestamp(value):
    s = _clean(value)
    if _is_missing(s):
        return NA
    dt = _parse_dt(s)
    if dt is None:
        return NA
    if dt.hour or dt.minute:
        return dt.strftime("%m/%d/%Y %H:%M")
    return dt.strftime("%m/%d/%Y")


def normalize_phone(value):
    s = re.sub(r"\.0$", "", _clean(value))  # Excel float artifact
    if _is_missing(s):
        return NA
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return "({}) {}-{}".format(digits[:3], digits[3:6], digits[6:])
    return digits if digits else NA  # invalid length: kept as digits, flagged in report


def is_valid_phone(value):
    return value == NA or bool(_PHONE_OK.fullmatch(value))


def normalize_zip(value):
    s = re.sub(r"\.0$", "", _clean(value))  # Excel float artifact
    if _is_missing(s):
        return NA
    digits = re.sub(r"\D", "", s)
    if len(digits) == 9:  # ZIP+4 without the dash
        return digits[:5]
    if 3 <= len(digits) <= 5:
        return digits.zfill(5)
    return NA


def normalize_homeowner(value):
    s = _clean(value).lower()
    if _is_missing(s):
        return NA
    if s in _OWNER_WORDS:
        return "Owner"
    if s in _RENTED_WORDS:
        return "Rented"
    if difflib.get_close_matches(s, list(_OWNER_WORDS), n=1, cutoff=0.8):
        return "Owner"
    if difflib.get_close_matches(s, list(_RENTED_WORDS), n=1, cutoff=0.8):
        return "Rented"
    return NA


def normalize_int(value):
    s = _clean(value)
    if _is_missing(s):
        return NA
    try:
        return str(int(float(s)))
    except ValueError:
        digits = re.sub(r"\D", "", s)
        return digits if digits else NA


def normalize_email(value):
    s = _clean(value)
    return NA if _is_missing(s) else s.lower()


def normalize_carrier(value):
    s = _clean(value)
    if _is_missing(s):
        return NA
    key = re.sub(r"[^a-z]", "", s.lower())
    if key in _CARRIER_KEYS:
        return _CARRIER_KEYS[key]
    close = difflib.get_close_matches(key, list(_CARRIER_KEYS), n=1, cutoff=0.75)
    if close:
        return _CARRIER_KEYS[close[0]]
    return normalize_name(s)  # unknown carrier: keep it, tidied up


_NORMALIZERS = {
    "email": normalize_email,
    "text": normalize_text,
    "name": normalize_name,
    "date": normalize_date,
    "datetime": normalize_timestamp,
    "phone": normalize_phone,
    "zip": normalize_zip,
    "homeowner": normalize_homeowner,
    "int": normalize_int,
    "carrier": normalize_carrier,
}


# ------------------------------------------------- grid (card) files

_GRID_NAME_RE = re.compile(r"^\s*name\b\s*[:=]?\s*[:=]?\s*(.*)$", re.I)
_GRID_DOB_RE = re.compile(r"^\s*d\.?\s*o\.?\s*b\.?\b\s*[:=]?\s*(.*)$", re.I)
_GRID_PHONE_RE = re.compile(r"^\s*(?:number|phone)\b\s*[:=]?\s*(.*)$", re.I)
_GRID_ADDR_RE = re.compile(r"^\s*address\b\s*[:=]?\s*(.*)$", re.I)
_GRID_CITY_RE = re.compile(r"^\s*city\s*[:=]\s*=?\s*(.*)$", re.I)
_GRID_STATE_RE = re.compile(r"^\s*state\s*[:=]\s*=?\s*(.*)$", re.I)
_GRID_ZIP_RE = re.compile(r"^\s*zip\s*_?\s*code?\s*[:=]\s*=?\s*(.*)$", re.I)
_GRID_INS_RE = re.compile(r"^\s*(?:insurance|inc)\b\s*[:=/]?\s*(.*)$", re.I)
_GRID_SKIP_RE = re.compile(
    r"live\s*call\s*transfer|lead\s*source|^\s*information|"
    r"^\s*\d+\s*(accidents?|tickets?|dui)\b|^\s*duration\b|only\s*home\s*insurance|"
    r"homeowners?\s*quote|^\s*(more\s*than|more\s*thena)|^\s*years?\s*[.:]|"
    r"^\s*\d+\s*(years?|yrs?|yr|yers)\.?\s*$|^\s*other\s*$|^\s*cars?\s*[:=]?\s*$",
    re.I)
_GRID_AUTOS_RE = re.compile(r"^\s*cars?\s*[:=]?\s*0?(\d)\s*$|^\s*0?(\d)\s*cars?\s*$", re.I)
_GRID_CAR_LABEL_RE = re.compile(r"^\s*cars?\b\s*[:=]?\s*", re.I)
_MAKE_RE = re.compile(
    r"(?<![A-Za-z])(chevrolet|chevy|checy|ford|toyota|honda|hunda|nissan|dodge|dogde|doge|"
    r"ram|jeep|kia|hyundai|hundai|hundayi|lexus|bmw|mercedes|mercedies|benz|audi|gmc|"
    r"buick|buic|cadillac|lincoln|mazda|subaru|volkswagen|volkswagon|volks\s*wag[ao]n|"
    r"acura|infiniti|tesla|rivian|volvo|chrysler|mitsubishi|saab|mercury|pontiac|"
    r"land\s*rover|landrover|range\s*rover|avalon|sienna)", re.I)
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_GA_ZIP_RE = re.compile(r"\b(3[01]\d{3})\b")


def _looks_like_grid(raw):
    """A 'grid' file stores each lead as a column of labeled cells."""
    names = dobs = 0
    for col in raw.columns:
        for v in raw[col].dropna():
            s = str(v)
            if _GRID_NAME_RE.match(s) and re.match(r"^\s*name\b", s, re.I):
                names += 1
            elif re.match(r"^\s*d\.?\s*o\.?\s*b\.?\b", s, re.I):
                dobs += 1
    return names >= 4 and dobs >= 4


def _grid_homeowner(text):
    low = text.strip().lower().lstrip(":= ").strip()
    if re.match(r"^(renter|renting|rented|rent)\b", low):
        return "Rented"
    m = re.match(r"^(home\s*own\w*|homeown\w*|homewoner|homeonwer|homeowner|owner)\b"
                 r"\s*:?\s*=?\s*(.*)$", low)
    if m:
        return "Rented" if re.match(r"^no\b", m.group(2)) else "Owner"
    if re.match(r"^single\s*family", low):
        return "Owner"
    return None


def _grid_clean_insurance(text):
    t = re.sub(r"\b(insurance|ins|inc)\b\.?", " ", text, flags=re.I)
    t = re.sub(r"\b(for|more\s+than|more\s+thena)\b", " ", t, flags=re.I)
    t = re.sub(r"\d+\s*(years?|yrs?|yr|yers|months?)\b\.?", " ", t, flags=re.I)
    t = re.sub(r"\b\d+\b", " ", t)
    return re.sub(r"[/:=.,]+", " ", t).strip()


_GRID_EMAIL_RE = re.compile(r"^\s*e-?mail\b\s*[:=]?\s*(.*)$", re.I)


def _parse_grid_lead(cells):
    """cells: the non-empty cell texts of one column within one block."""
    name = dob = phone = city = state = zipc = homeowner = autos = carrier = None
    email = None
    addr_parts, vehicles = [], []

    for t in cells:
        m = _GRID_EMAIL_RE.match(t)
        if m:
            if email is None and "@" in m.group(1):
                email = m.group(1).strip()
            continue
        if _GRID_SKIP_RE.search(t):
            continue
        m = _GRID_NAME_RE.match(t)
        if m and re.match(r"^\s*name\b", t, re.I):
            if name is None and m.group(1).strip(" :=,"):
                name = m.group(1).strip(" :=,")
            continue
        m = _GRID_DOB_RE.match(t)
        if m:
            if dob is None and m.group(1).strip():
                dob = re.sub(r"\s+", "", m.group(1).replace(";", "/"))
            continue
        m = _GRID_PHONE_RE.match(t)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if phone is None and digits:
                phone = digits
            continue
        m = _GRID_ADDR_RE.match(t)
        if m:
            if m.group(1).strip(" :=,"):
                addr_parts.append(m.group(1).strip(" ,"))
            continue
        m = _GRID_CITY_RE.match(t)
        if m:
            if city is None and m.group(1).strip(" :=,"):
                city = m.group(1).strip(" :=,")
            continue
        m = _GRID_STATE_RE.match(t)
        if m:
            if state is None and m.group(1).strip(" :=,"):
                state = m.group(1).strip(" :=,")
            continue
        m = _GRID_ZIP_RE.match(t)
        if m:
            z = re.search(r"\d{5}", m.group(1))
            if zipc is None and z:
                zipc = z.group(0)
            continue
        ho = _grid_homeowner(t)
        if ho:
            if homeowner is None:
                homeowner = ho
            continue
        m = _GRID_AUTOS_RE.match(t)
        if m:
            if autos is None:
                autos = m.group(1) or m.group(2)
            continue
        # vehicle: needs a make AND a year somewhere in the text
        if _MAKE_RE.search(t) and _YEAR_RE.search(t):
            v = _GRID_CAR_LABEL_RE.sub("", t).strip()
            for pat, canon in _CARRIER_PATTERNS:  # "...RAM Insurance:" tails
                cm = pat.search(v)
                if cm:
                    if carrier is None:
                        carrier = canon
                    v = v[:cm.start()].strip(" ,/")
                    break
            v = re.sub(r"\binsurance\b\s*:?\s*$", "", v, flags=re.I).strip(" ,/")
            v = " ".join(v.split("\n")[0].split())
            if v and len(vehicles) < 4:
                vehicles.append(v)
            continue
        matched = False
        for pat, canon in _CARRIER_PATTERNS:
            if pat.search(t):
                if carrier is None:
                    carrier = canon
                matched = True
                break
        if matched:
            continue
        m = _GRID_INS_RE.match(t)
        if m:
            cleaned = _grid_clean_insurance(m.group(1))
            if carrier is None and cleaned:
                carrier = cleaned
            continue
        if re.fullmatch(r"(ga|georgia)\.?", t.strip(" ,"), re.I):
            if state is None:
                state = "GA"
            continue
        if _GA_ZIP_RE.search(t) and re.search(r"[A-Za-z]", t):
            addr_parts.append(t.strip(" ,"))
            if zipc is None:
                zipc = _GA_ZIP_RE.search(t).group(1)
            continue
        if re.match(r"^\d+\s+\S+", t):
            addr_parts.append(t.strip(" ,"))
            continue

    if name is None:
        return None
    if state and state.lower() in ("ga", "georgia", "ga."):
        state = "GA"
    addr = [p for p in addr_parts if p.strip(" ,")][:3]
    if city and not any(city.lower() in p.lower() for p in addr):
        addr.append(city)
    if state and not any(re.search(r"\b" + re.escape(state) + r"\b", p, re.I) for p in addr):
        addr.append(state)
    if zipc is None:
        for p in addr_parts:
            z = _GA_ZIP_RE.search(p)
            if z:
                zipc = z.group(1)
                break
    return {
        "Full Name": name,
        "Email": email or "",
        "Date of Birth": dob or "",
        "Phone Number": phone or "",
        "Address": ", ".join(x.strip(" ,") for x in addr if x.strip(" ,")),
        "ZIP Code": zipc or "",
        "Homeowner": homeowner or "",
        "Autos": autos if autos is not None else (str(len(vehicles)) if vehicles else ""),
        "Current Insurance": carrier or "",
        "Cars Make and Model": " / ".join(vehicles),
    }


def _parse_grid(raw):
    """Flatten a grid file into one row per lead. Order: banner-separated
    blocks top to bottom, and within a block, columns left to right."""
    nrows, ncols = raw.shape

    def cell(r, c):
        v = raw.iat[r, c]
        return "" if pd.isna(v) else str(v).strip()

    banner_rows = []
    for r in range(nrows):
        hits = sum("live call transfer" in cell(r, c).lower() for c in range(ncols))
        if hits >= 3:
            banner_rows.append(r)

    starts = [0] + [b + 1 for b in banner_rows]
    ends = banner_rows + [nrows]
    blocks = [(s, e) for s, e in zip(starts, ends) if e > s]

    leads = []
    for s, e in blocks:
        for c in range(ncols):
            cells = [cell(r, c) for r in range(s, e) if cell(r, c)]
            if not cells:
                continue
            lead = _parse_grid_lead(cells)
            if lead:
                leads.append(lead)
    frame = pd.DataFrame(leads)
    return frame, len(banner_rows)


# ------------------------------------------ linear (plain-text) card files

_LINEAR_BANNER_RE = re.compile(r"^\s*live\s*call\s*transfer\s*:?\s*$", re.I)
_LINEAR_SEP_RE = re.compile(r"^_{5,}$")


def _looks_like_linear_cards(text):
    """A 'linear' card file: each lead is one label-per-line block ('Name:',
    'DOB:', ...) in a plain text stream, one lead after another — as opposed
    to the grid layout's one-lead-per-spreadsheet-column. Has no reliable
    delimiter, so it must be detected (and parsed) before any CSV/delimiter
    sniffing runs on it."""
    names = dobs = 0
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^\s*name\b\s*[:=]", s, re.I):
            names += 1
        elif re.match(r"^\s*d\.?\s*o\.?\s*b\.?\b\s*[:=]", s, re.I):
            dobs += 1
    return names >= 4 and dobs >= 4


def _parse_linear_cards(text):
    """Split into blocks on banner/underscore-rule lines, then reuse the
    same per-line field extraction as the grid parser (each text line here
    plays the role of one grid cell)."""
    blocks, current = [], []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if _LINEAR_BANNER_RE.match(line) or _LINEAR_SEP_RE.match(line):
            if current:
                blocks.append(current)
                current = []
            continue
        if line:
            current.append(line)
    if current:
        blocks.append(current)

    leads = [lead for cells in blocks
             for lead in [_parse_grid_lead(cells)] if lead]
    frame = pd.DataFrame(leads)
    return frame, len(blocks)


# --------------------------------------- labeled-column (form export) files

# CRM/quoting-tool exports where each field is its OWN row (the label text
# repeated across every lead's column, e.g. "First Name"), with the actual
# per-lead value on the row(s) below. Spacing between a label and its value
# varies per lead, and — because leads carry a different number of optional
# sub-records (spouse, additional drivers, violations, vehicles) — columns
# drift out of row-alignment with each other partway through, sometimes
# within the very same lead. So this is parsed per COLUMN, independently,
# as a flat sequence of non-blank cell "tokens" walked for label->value
# pairs, rather than by any shared row index.
_LABELED_CORE_FIELDS = {
    "first name": "first", "last name": "last",
    "primary phone": "phone", "phone": "phone", "phone number": "phone",
    "email": "email",
    "address": "address", "city": "city", "state": "state",
    "zipcode": "zip", "zip code": "zip",
    "date of birth": "dob",
}
_LABELED_SECTION_BOUNDARY = {"drivers"}


def _looks_like_labeled_columns(raw):
    hits_banner = hits_first = 0
    for col in raw.columns:
        for v in raw[col].dropna():
            s = str(v).strip().lower()
            if s == "contact details":
                hits_banner += 1
            elif s == "first name":
                hits_first += 1
    return hits_banner >= 1 and hits_first >= 4


def _parse_labeled_column(tokens):
    # the identifying "Contact Details" fields end at the first sub-section
    # boundary (e.g. "Drivers") — later repeats of Name/DOB for a driver or
    # violation record must not overwrite the primary contact's own values
    scope_end = next((i for i, t in enumerate(tokens)
                       if t.strip().lower() in _LABELED_SECTION_BOUNDARY), len(tokens))
    fields = {}
    i = 0
    while i < scope_end:
        key = _LABELED_CORE_FIELDS.get(tokens[i].strip().lower())
        if key and key not in fields and i + 1 < scope_end:
            val = tokens[i + 1].strip()
            if val.lower() not in _LABELED_CORE_FIELDS:
                fields[key] = val
            i += 2
            continue
        i += 1

    name = " ".join(p for p in (fields.get("first"), fields.get("last")) if p).strip()
    if not name:
        return None

    # vehicles: a Year/Make/Model run can appear anywhere further down the
    # column (inside a "Vehicles"/"Add Vehicle" sub-section) — best-effort,
    # not bounded to a bulletproof sub-scope given how deeply this format nests
    vehicles = []
    year = make = model = None
    for j, t in enumerate(tokens):
        low = t.strip().lower()
        if low == "year" and j + 1 < len(tokens):
            year = tokens[j + 1].strip()
        elif low == "make" and j + 1 < len(tokens):
            make = tokens[j + 1].strip()
        elif low == "model" and j + 1 < len(tokens):
            model = tokens[j + 1].strip()
            if make and model.lower() != "model" and len(vehicles) < 4:
                vehicles.append(" ".join(
                    p for p in (year, make, model) if p and p.lower() not in ("year", "make")))
            year = make = model = None

    carrier = None
    for t in tokens[scope_end:]:
        for pat, canon in _CARRIER_PATTERNS:
            if pat.search(t):
                carrier = canon
                break
        if carrier:
            break

    addr = [p for p in (fields.get("address"), fields.get("city"), fields.get("state")) if p]
    return {
        "Full Name": name,
        "Email": fields.get("email") or "",
        "Date of Birth": fields.get("dob") or "",
        "Phone Number": fields.get("phone") or "",
        "Address": ", ".join(addr),
        "ZIP Code": fields.get("zip") or "",
        "Homeowner": "",  # not reliably recoverable — see notes in the PR/README
        "Autos": str(len(vehicles)) if vehicles else "",
        "Current Insurance": carrier or "",
        "Cars Make and Model": " / ".join(vehicles),
    }


def _parse_labeled_columns(raw):
    """A column can stack MULTIPLE leads if "Contact Details" repeats down
    it (the file has more leads than fit in one row-block) — split on that
    before parsing so a later lead's fields (and vehicles) never bleed into
    an earlier one's."""
    leads = []
    for c in range(raw.shape[1]):
        tokens = [str(v).strip() for v in raw[c] if not pd.isna(v) and str(v).strip()]
        starts = [i for i, t in enumerate(tokens) if t.strip().lower() == "contact details"]
        if not starts:
            starts = [0]
        for s, e in zip(starts, starts[1:] + [len(tokens)]):
            lead = _parse_labeled_column(tokens[s:e])
            if lead:
                leads.append(lead)
    frame = pd.DataFrame(leads)
    return frame, 0


# --------------------------------------- verifier/dialer dashboard scrapes

# A CSV export of a lead-verifier web app's rendered table: real lead data
# is interleaved with UI chrome (button labels like "Refresh", dropdown
# placeholders like "Select Agent"/"Select State", record UUIDs, footer
# text, nav-menu labels once a column runs out of real leads). Each lead's
# card is bounded by a "Refresh" button label; the phone number that
# triggered the transfer sits just BEFORE that "Refresh", not inside the
# card itself. An "Agent Info" section right after each card repeats
# "Full Name" for the AGENT, not the lead, so core-field extraction must
# stop there.
_VERIFIER_CORE_FIELDS = {
    "full name": "name", "date of birth": "dob", "address": "address",
    "current auto carrier": "carrier", "home owner": "homeowner",
}
_VERIFIER_SECTION_BOUNDARY = {"agent info"}
_VERIFIER_VEHICLE_LABEL = "make and model"
_VERIFIER_KNOWN_LABELS = {
    "phone number", "no transfer found.", "select agent", "select state",
    "refresh", "full name", "date of birth", "address", "spouse",
    "auto & home info", "current auto carrier", "any lapses",
    "make and model", "accidents", "tickets", "home owner", "home type",
    "current home carrier", "any claim in past 3 years",
    "transfer lead reset form", "agent info", "states", "verifier notes",
    "lead info",
}
_VERIFIER_DURATION_RE = re.compile(
    r"\s*[-–]?\s*(more\s*(than|then)\s*)?\d+\+?\s*(years?|yrs?|months?)\.?\s*$", re.I)


def _looks_like_verifier_scrape(raw):
    hits_refresh = hits_full = 0
    for col in raw.columns:
        for v in raw[col].dropna():
            s = str(v).strip().lower()
            if s == "refresh":
                hits_refresh += 1
            elif s == "full name":
                hits_full += 1
    return hits_refresh >= 4 and hits_full >= 4


def _parse_verifier_card(tokens):
    """tokens: one lead's card, already sliced between its "Refresh" and
    the next one (or the section boundary within it)."""
    scope_end = next((i for i, t in enumerate(tokens)
                       if t.strip().lower() in _VERIFIER_SECTION_BOUNDARY), len(tokens))
    fields = {}
    i = 0
    while i < scope_end:
        key = _VERIFIER_CORE_FIELDS.get(tokens[i].strip().lower())
        if key and key not in fields and i + 1 < scope_end:
            val = tokens[i + 1].strip()
            if val.lower() not in _VERIFIER_KNOWN_LABELS:
                fields[key] = val
            i += 2
            continue
        i += 1

    name = (fields.get("name") or "").strip()
    if not name:
        return None

    # "Make And Model" is followed by one or more raw vehicle description
    # lines (not broken into Year/Make/Model), until the next known label
    vehicles = []
    j = 0
    while j < scope_end:
        if tokens[j].strip().lower() == _VERIFIER_VEHICLE_LABEL:
            k = j + 1
            while (k < scope_end and len(vehicles) < 4
                   and tokens[k].strip().lower() not in _VERIFIER_KNOWN_LABELS):
                vehicles.append(tokens[k].strip())
                k += 1
            j = k
            continue
        j += 1

    carrier = _VERIFIER_DURATION_RE.sub("", fields.get("carrier") or "").strip(" -")
    address = fields.get("address") or ""
    zip_match = re.search(r"(\d{5})(?:-\d{4})?\s*$", address)

    return {
        "Full Name": name,
        "Email": "",
        "Date of Birth": fields.get("dob") or "",
        "Phone Number": "",  # filled in by the caller from the preamble
        "Address": address,
        "ZIP Code": zip_match.group(1) if zip_match else "",
        "Homeowner": fields.get("homeowner") or "",
        "Autos": str(len(vehicles)) if vehicles else "",
        "Current Insurance": carrier,
        "Cars Make and Model": " / ".join(vehicles),
    }


def _parse_verifier_scrape(raw):
    leads = []
    for c in range(raw.shape[1]):
        tokens = [str(v).strip() for v in raw[c] if not pd.isna(v) and str(v).strip()]
        refresh_at = [i for i, t in enumerate(tokens) if t.strip().lower() == "refresh"]
        for idx, r in enumerate(refresh_at):
            end = refresh_at[idx + 1] if idx + 1 < len(refresh_at) else len(tokens)
            lead = _parse_verifier_card(tokens[r + 1:end])
            if lead is None:
                continue
            # the phone number that triggered this transfer sits in the
            # short preamble just before "Refresh", not inside the card
            phone = None
            for i in range(r - 1, max(-1, r - 15), -1):
                if tokens[i].strip().lower() == "phone number" and i + 1 < r:
                    phone = tokens[i + 1].strip()
                    break
            lead["Phone Number"] = phone or ""
            leads.append(lead)
    frame = pd.DataFrame(leads)
    return frame, 0


# ------------------------------------------------- headerless files

def _find_header_row(raw, schema):
    """Scan the first rows for the one that looks most like a header."""
    alias_to_col = _alias_map(schema)
    drop = {_norm_header(d) for d in schema.get("drop_columns", [])}
    best_row, best_hits = None, 0
    for r in range(min(len(raw), 10)):
        hits = 0
        for v in raw.iloc[r]:
            if pd.isna(v):
                continue
            n = _norm_header(v)
            if n in alias_to_col or n in drop:
                hits += 1
        if hits > best_hits:
            best_row, best_hits = r, hits
    return best_row if best_hits >= 2 else None


def _frame_from_raw(raw, schema):
    """Turn a header-less read into a headed DataFrame.

    Returns (df, inferred) — inferred is True when no header row existed and
    columns had to be deduced from the data content.
    """
    if _looks_like_grid(raw):
        frame, banners = _parse_grid(raw)
        return frame, True, banners
    if _looks_like_labeled_columns(raw):
        frame, skipped = _parse_labeled_columns(raw)
        return frame, True, skipped
    if _looks_like_verifier_scrape(raw):
        frame, skipped = _parse_verifier_scrape(raw)
        return frame, True, skipped
    header_row = _find_header_row(raw, schema)
    if header_row is not None:
        headers, seen = [], {}
        for j, v in enumerate(raw.iloc[header_row]):
            h = str(v) if not pd.isna(v) else f"column_{j}"
            if h in seen:
                seen[h] += 1
                h = f"{h}.{seen[h]}"
            else:
                seen[h] = 0
            headers.append(h)
        df = raw.iloc[header_row + 1:].reset_index(drop=True)
        df.columns = headers
        return df, False, header_row
    frame, skipped = _infer_frame(raw)
    return frame, True, skipped


def _infer_frame(raw):
    """No header row: classify each column by its content and build a frame
    whose columns already carry the schema names. Split-up fields (first/last
    name, street/city/state, multiple vehicle columns) are recombined.

    Returns (frame, skipped) — skipped counts legend/junk rows that had too
    few cells to be a lead."""
    data = raw[raw.notna().sum(axis=1) >= 3].reset_index(drop=True)
    skipped = len(raw) - len(data)
    if data.empty:
        return pd.DataFrame(), skipped
    ncols = data.shape[1]

    samples = {}
    for c in range(ncols):
        vals = [str(v).strip() for v in data[c].dropna()]
        samples[c] = [v for v in vals if v and not _is_missing(v)][:200]

    def ratio(c, pred):
        vals = samples[c]
        return sum(1 for v in vals if pred(v)) / len(vals) if vals else 0.0

    def is_phone(v):
        d = re.sub(r"\D", "", v)
        return len(d) == 10 or (len(d) == 11 and d.startswith("1"))

    def is_zip(v):
        return bool(re.fullmatch(r"\d{5}(-\d{4})?", v))

    def is_date(v):
        return bool(re.search(r"[/\-]", v)) and _parse_dt(v) is not None

    def is_small_int(v):
        return bool(re.fullmatch(r"\d", v))

    def is_carrier(v):
        key = re.sub(r"[^a-z]", "", v.lower())
        return key in _CARRIER_KEYS or bool(
            difflib.get_close_matches(key, list(_CARRIER_KEYS), n=1, cutoff=0.8))

    _EXPLICIT_HOME = {"own", "owner", "owned", "owns", "homeowner",
                      "rent", "rented", "renter", "rents"}

    def is_home(v):
        return v.lower() in _EXPLICIT_HOME

    def is_vehicle(v):
        return bool(re.match(r"(19|20)\d{2}\s+\S", v))

    def is_alpha(v):
        return len(v) <= 40 and bool(re.fullmatch(r"[A-Za-z][A-Za-z .,'\-]*", v))

    def is_street(v):
        return bool(re.match(r"\d+\s+\S+", v))

    used = set()
    single = {}   # schema column -> source col index

    def claim(schema_col, pred, threshold, best=False):
        pick, pick_r = None, 0.0
        for c in range(ncols):
            if c in used:
                continue
            r = ratio(c, pred)
            if r >= threshold:
                if not best:
                    pick = c
                    break
                if r > pick_r:
                    pick, pick_r = c, r
        if pick is not None:
            single[schema_col] = pick
            used.add(pick)
        return pick

    # phones first: claim the real one, retire duplicates of it
    phone_col = claim("Phone Number", is_phone, 0.7)
    if phone_col is not None:
        for c in range(ncols):
            if c not in used and ratio(c, is_phone) >= 0.7:
                used.add(c)

    claim("Email", lambda v: "@" in v and "." in v.rsplit("@", 1)[-1], 0.5)

    claim("ZIP Code", is_zip, 0.7, best=True)

    # date columns: birth-year medians are DOBs (first one wins; a second is
    # likely a co-applicant and is ignored), recent medians are timestamps
    date_cols = [c for c in range(ncols) if c not in used and ratio(c, is_date) >= 0.7]
    if date_cols:
        def median_year(c):
            years = sorted(_parse_dt(v).year for v in samples[c][:50]
                           if _parse_dt(v) is not None)
            return years[len(years) // 2] if years else 9999
        date_cols.sort(key=median_year)
        used.update(date_cols)
        dobs = [c for c in date_cols if median_year(c) < 2015]
        recents = [c for c in date_cols if median_year(c) >= 2015]
        if dobs:
            single["Date of Birth"] = dobs[0]
        if recents:
            single["Timestamp"] = recents[0]

    claim("Autos", is_small_int, 0.7)
    claim("Current Insurance", is_carrier, 0.5)
    claim("Homeowner", is_home, 0.6)

    vehicle_cols = [c for c in range(ncols)
                    if c not in used and ratio(c, is_vehicle) >= 0.5][:3]
    used.update(vehicle_cols)

    # name: first alpha column from the left; two adjacent = first + last name
    name_cols = []
    for c in range(ncols):
        if c not in used and ratio(c, is_alpha) >= 0.8:
            name_cols = [c]
            if c + 1 < ncols and c + 1 not in used and ratio(c + 1, is_alpha) >= 0.8:
                name_cols.append(c + 1)
            break
    used.update(name_cols)

    # address: street column, plus an adjacent city and 2-letter state if present
    address_cols = []
    street = claim("Address", is_street, 0.6)
    if street is not None:
        del single["Address"]
        address_cols = [street]
        nxt = street + 1
        if nxt < ncols and nxt not in used and ratio(nxt, is_alpha) >= 0.8:
            address_cols.append(nxt)
            used.add(nxt)
            nxt += 1
        if nxt < ncols and nxt not in used and ratio(
                nxt, lambda v: bool(re.fullmatch(r"[A-Za-z]{2}", v))) >= 0.8:
            address_cols.append(nxt)
            used.add(nxt)

    def joined(cols, sep):
        parts = data[cols].fillna("")
        return [
            sep.join(p.strip() for p in row if p.strip() and not _is_missing(p.strip()))
            for row in parts.itertuples(index=False)
        ]

    out = pd.DataFrame(index=data.index)
    for schema_col, c in single.items():
        out[schema_col] = data[c].fillna("")
    if name_cols:
        out["Full Name"] = joined(name_cols, " ")
    if address_cols:
        out["Address"] = joined(address_cols, ", ")
    if vehicle_cols:
        out["Cars Make and Model"] = joined(vehicle_cols, " / ")
    return out, skipped


# ---------------------------------------------------------------- organize

_XLSX_MAGIC = b"PK\x03\x04"          # zip container (xlsx/xlsm)
_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE2 (legacy .xls)


def _read_raw(path):
    """Read any tabular file into a raw headerless frame, plus the decoded
    text for text files (None for Excel) — the text lets callers detect a
    non-tabular layout (e.g. linear card sheets) before delimiter-sniffing
    has a chance to mangle it.

    File type is detected from CONTENT, not extension — a .csv that is
    really an XLSX parses fine. Text files get encoding + delimiter
    auto-detection (CSV, TSV, semicolon, pipe)."""
    with open(path, "rb") as f:
        head = f.read(8)

    if head[:4] == _XLSX_MAGIC or head == _XLS_MAGIC:
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=str)
        frames = [f for f in sheets.values() if not f.empty]
        return (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()), None

    with open(path, "rb") as f:
        data = f.read()
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None or not text.strip():
        return pd.DataFrame(), None

    sep = ","
    try:
        sep = _csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
    except _csv.Error:
        pass
    try:
        raw = pd.read_csv(io.StringIO(text), dtype=str, header=None,
                          skip_blank_lines=False, sep=sep, engine="python")
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        raw = pd.DataFrame()
    return raw, text


def organize_file(path, schema):
    raw, text = _read_raw(path)

    if text is not None and _looks_like_linear_cards(text):
        df, blocks = _parse_linear_cards(text)
        inferred, blank_rows = True, 0
        skipped = max(blocks - len(df), 0)  # preamble/junk blocks with no lead
    else:
        blank_rows = 0
        if not raw.empty:
            blank = raw.isna().all(axis=1)
            blank_rows = int(blank.sum())
            raw = raw[~blank].reset_index(drop=True)
        if raw.empty:
            return organize_dataframe(pd.DataFrame(), schema)
        df, inferred, skipped = _frame_from_raw(raw, schema)

    out, report = organize_dataframe(df, schema)
    report["header_inferred"] = inferred
    report["skipped_non_lead_rows"] = blank_rows + skipped

    try:  # refinement layer: rules, typo suggestions, fuzzy dedup, scoring
        import refinement
        refinement.refine(out, report, schema)
    except (ImportError, FileNotFoundError):
        pass  # organizer stays usable without the refinement layer
    return out, report


_DIFF_REASONS = {
    "name": "name recased/cleaned",
    "date": "date normalized to MM/DD/YYYY",
    "datetime": "timestamp normalized",
    "phone": "phone formatted",
    "zip": "ZIP normalized",
    "homeowner": "homeowner standardized",
    "int": "count normalized",
    "carrier": "carrier canonicalized",
    "email": "email normalized",
    "text": "whitespace cleaned",
}


def organize_dataframe(df, schema):
    """Clean a raw leads DataFrame into the schema. Returns (df, report)."""
    columns = [c["name"] for c in schema["columns"]]
    aux_columns = [c["name"] for c in schema.get("aux_columns", [])]
    types = {c["name"]: c.get("type", "text")
             for c in schema["columns"] + schema.get("aux_columns", [])}

    report = {
        "input_rows": 0,
        "output_rows": 0,
        "duplicates_removed": 0,
        "removed_duplicates": [],
        "empty_rows_skipped": 0,
        "skipped_non_lead_rows": 0,
        "header_inferred": False,
        "invalid_phone_rows": [],
        "column_issues": {c: {"fixed": 0, "na": 0} for c in columns},
        "dropped_headers": [],
        "unmapped_headers": [],
        "row_diffs": [],
        "aux_data": pd.DataFrame(),
        "timestamp_synthesized": False,
    }

    if df.empty:
        return pd.DataFrame(columns=columns), report

    df = df.dropna(how="all").reset_index(drop=True)
    report["input_rows"] = int(len(df))

    mapping, dropped, unmapped, compose = map_headers(df.columns, schema)
    report["dropped_headers"] = dropped
    report["unmapped_headers"] = unmapped
    source_for = {target: orig for orig, target in mapping.items()}

    diffs = []
    processed_at = datetime.now().strftime("%m/%d/%Y %H:%M")
    out = pd.DataFrame(index=df.index)
    for col in columns + aux_columns:
        source = source_for.get(col)
        parts = compose.get(col)
        if source is None and parts is None and col in aux_columns:
            continue  # aux fields are optional; only carry them when present
        no_source = source is None and parts is None
        # the source file has no timestamp/date-added field at all — stamp
        # every row with the time this file was processed instead of NA
        synth_timestamp = no_source and col == "Timestamp" and types[col] == "datetime"
        if synth_timestamp:
            report["timestamp_synthesized"] = True
        normalize = _NORMALIZERS[types[col]]
        issues = report["column_issues"].get(col)
        part_sep = " " if types[col] == "name" else ", "
        values = []
        for i in df.index:
            raw = ""
            if parts is not None:
                bits = []
                for p in parts:
                    if p is not None and not pd.isna(df.at[i, p]):
                        b = str(df.at[i, p]).strip()
                        if b and not _is_missing(b):
                            bits.append(b)
                raw = part_sep.join(bits)
            elif source is not None and not pd.isna(df.at[i, source]):
                raw = str(df.at[i, source])
            elif synth_timestamp:
                raw = processed_at
            val = normalize(raw)
            if issues is not None:
                if val == NA:
                    issues["na"] += 1
                elif raw.strip() != val:
                    issues["fixed"] += 1
            if col in report["column_issues"] and raw.strip() and raw.strip() != val:
                reason = (_DIFF_REASONS.get(types[col], "normalized")
                          if val != NA else "unparseable — set to NA")
                diffs.append({"src": int(i), "field": col,
                              "original": raw.strip(), "new": val,
                              "reason": reason})
            values.append(val)
        out[col] = values
    out["_src"] = list(df.index)

    all_na = (out[columns] == NA).all(axis=1)
    if all_na.any():
        report["empty_rows_skipped"] = int(all_na.sum())
        out = out[~all_na].reset_index(drop=True)

    # a duplicate must match on EVERY dedupe key (e.g. phone AND name), so two
    # different people sharing a phone number are both kept
    dedupe_on = schema.get("dedupe_on")
    if dedupe_on:
        keys = [dedupe_on] if isinstance(dedupe_on, str) else list(dedupe_on)
        keys = [k for k in keys if k in out.columns]
        if keys:
            folded = out[keys].apply(lambda s: s.str.lower().str.strip())
            dup = folded.duplicated(keep="first") & (out[keys] != NA).all(axis=1)
            report["duplicates_removed"] = int(dup.sum())
            label_cols = list(dict.fromkeys(
                (["Full Name"] if "Full Name" in out.columns else []) + keys))
            report["removed_duplicates"] = [
                {k: row[k] for k in label_cols} for _, row in out[dup].iterrows()
            ]
            out = out[~dup].reset_index(drop=True)

    sort_by = [k for k in schema.get("sort_by", []) if k in out.columns]
    if sort_by and len(out):
        keys = pd.DataFrame(index=out.index)
        for k in sort_by:
            if types.get(k) in ("date", "datetime"):
                keys[k] = pd.to_datetime(
                    out[k].where(out[k] != NA), errors="coerce", format="mixed"
                )
            else:
                keys[k] = out[k]
        out = out.loc[keys.sort_values(sort_by, kind="stable").index].reset_index(drop=True)

    if "Phone Number" in out.columns:
        report["invalid_phone_rows"] = [
            int(i) for i, v in out["Phone Number"].items() if not is_valid_phone(v)
        ]

    # remap per-cell diffs to final row numbers (1-based); rows that were
    # dropped as duplicates/empty are reported elsewhere, not in the diff
    src_to_row = {int(s): pos + 1 for pos, s in enumerate(out["_src"])}
    report["row_diffs"] = [
        {"row": src_to_row[d["src"]], "field": d["field"],
         "original": d["original"], "new": d["new"], "reason": d["reason"]}
        for d in diffs if d["src"] in src_to_row
    ]

    aux_present = [c for c in aux_columns if c in out.columns]
    report["aux_data"] = out[aux_present].reset_index(drop=True)
    out = out[columns].reset_index(drop=True)

    report["output_rows"] = int(len(out))
    return out, report

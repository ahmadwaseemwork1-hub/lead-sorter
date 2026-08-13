"""Lead refinement layer: cross-field rules, typo correction, fuzzy dedup,
and per-lead confidence scoring.

Runs AFTER the organizer has produced the cleaned frame; it never mutates the
cleaned output columns. Everything it finds goes into the report (violations,
suggestions, duplicate groups, scores) — the only value it may rewrite is the
auxiliary Email field (not part of the output CSV), and only when the fix
confidence clears the configured bar, in which case the change is logged.

All thresholds, rule switches, penalties, nicknames, verticals, and known
email domains live in schemas/refinement.json.
"""

import difflib
import json
import os
import re
from datetime import date

NA = "NA"

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "schemas", "refinement.json")
_config_cache = None


def load_config(path=None):
    global _config_cache
    if path is None:
        if _config_cache is None:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                _config_cache = json.load(f)
        return _config_cache
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- vertical

def detect_vertical(headers, cfg):
    hset = {str(h).lower().strip() for h in headers}
    best, best_hits = "general", 0
    for vertical, signatures in cfg.get("verticals", {}).items():
        hits = sum(1 for s in signatures if s in hset)
        if hits > best_hits:
            best, best_hits = vertical, hits
    return best if best_hits >= cfg.get("vertical_min_hits", 2) else "general"


# ---------------------------------------------------------------- helpers

def _aux_series(aux, name):
    if aux is None or not hasattr(aux, "columns") or name not in aux.columns:
        return None
    return aux[name]


def _age_from_dob(dob):
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})", str(dob))
    if not m:
        return None
    try:
        born = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _canon_name(name, nicknames):
    s = re.sub(r"[^a-z ]", "", str(name).lower()).strip()
    parts = s.split()
    if parts:
        parts[0] = nicknames.get(parts[0], parts[0])
    return " ".join(parts)


# ---------------------------------------------------------------- emails

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def email_checks(aux, cfg):
    """Validate aux emails; suggest/apply domain-typo fixes. Returns
    (corrections, violations). Applied fixes rewrite the aux value only."""
    corrections, violations = [], []
    emails = _aux_series(aux, "Email")
    if emails is None:
        return corrections, violations
    domains = cfg.get("email_domains", [])
    suggest = cfg.get("email_suggest_ratio", 0.75)
    autofix = cfg.get("email_autofix_ratio", 0.85)
    for pos in range(len(emails)):
        e = str(emails.iloc[pos])
        if e in (NA, "", "nan"):
            continue
        row = pos + 1
        if not _EMAIL_RE.fullmatch(e):
            violations.append({"row": row, "rule": "email_invalid",
                               "fields": ["Email"],
                               "message": f"not a valid email: {e}"})
            continue
        domain = e.rsplit("@", 1)[1].lower()
        if domain in domains:
            continue
        close = difflib.get_close_matches(domain, domains, n=1, cutoff=suggest)
        if not close:
            continue
        ratio = difflib.SequenceMatcher(None, domain, close[0]).ratio()
        fixed = e.rsplit("@", 1)[0] + "@" + close[0]
        applied = ratio >= autofix
        if applied:
            aux.iat[pos, aux.columns.get_loc("Email")] = fixed
        corrections.append({"row": row, "field": "Email", "original": e,
                            "suggested": fixed, "applied": applied,
                            "confidence": round(ratio, 2)})
        violations.append({"row": row, "rule": "email_domain_typo",
                           "fields": ["Email"],
                           "message": f"{domain} looks like {close[0]}"})
    return corrections, violations


# ---------------------------------------------------------------- rules

def rule_checks(df, aux, cfg):
    """Cross-field validation. Rules only report; they never mutate data."""
    rules = cfg.get("rules", {})
    violations = []

    def on(name):
        return rules.get(name, {}).get("enabled", False)

    dobs = df["Date of Birth"] if "Date of Birth" in df.columns else None
    names = df["Full Name"] if "Full Name" in df.columns else None
    ages_col = _aux_series(aux, "Age")
    occ_col = _aux_series(aux, "Occupation")

    for pos in range(len(df)):
        row = pos + 1
        age = _age_from_dob(dobs.iloc[pos]) if dobs is not None else None

        if on("dob_implausible") and age is not None:
            lo = rules["dob_implausible"].get("min_age", 16)
            hi = rules["dob_implausible"].get("max_age", 105)
            if not lo <= age <= hi:
                violations.append({"row": row, "rule": "dob_implausible",
                                   "fields": ["Date of Birth"],
                                   "message": f"derived age {age} outside {lo}-{hi}"})

        if on("dob_vs_age") and age is not None and ages_col is not None:
            stated = str(ages_col.iloc[pos])
            if stated not in (NA, "", "nan"):
                try:
                    stated_age = int(float(stated))
                    tol = rules["dob_vs_age"].get("tolerance_years", 2)
                    if abs(stated_age - age) > tol:
                        violations.append({
                            "row": row, "rule": "dob_vs_age",
                            "fields": ["Date of Birth", "Age"],
                            "message": f"DOB implies {age} but Age column says {stated_age}"})
                except ValueError:
                    pass

        if on("dob_vs_occupation") and age is not None and occ_col is not None:
            occ = str(occ_col.iloc[pos]).lower()
            r = rules["dob_vs_occupation"]
            if "student" in occ and age > r.get("student_max_age", 60):
                violations.append({"row": row, "rule": "dob_vs_occupation",
                                   "fields": ["Date of Birth", "Occupation"],
                                   "message": f"occupation 'student' but derived age {age}"})
            if "retired" in occ and age < r.get("retired_min_age", 40):
                violations.append({"row": row, "rule": "dob_vs_occupation",
                                   "fields": ["Date of Birth", "Occupation"],
                                   "message": f"occupation 'retired' but derived age {age}"})

        if on("name_suspicious") and names is not None:
            nm = str(names.iloc[pos])
            if nm != NA and (re.search(r"\d", nm) or len(nm.replace(" ", "")) < 2):
                violations.append({"row": row, "rule": "name_suspicious",
                                   "fields": ["Full Name"],
                                   "message": f"name looks malformed: {nm}"})
    return violations


# ---------------------------------------------------------------- dedup

def find_duplicate_groups(df, aux, cfg):
    """Fuzzy duplicate detection: block on phone/email, then fuzzy-compare
    names with nickname handling. Flags groups — never deletes rows."""
    if df is None or len(df) == 0 or "Full Name" not in df.columns:
        return [], {}
    nicknames = cfg.get("nicknames", {})
    threshold = cfg.get("name_fuzzy_threshold", 0.85)
    emails = _aux_series(aux, "Email")

    blocks = {}
    for pos in range(len(df)):
        phone = str(df.iloc[pos].get("Phone Number", NA))
        digits = re.sub(r"\D", "", phone) if phone != NA else ""
        if digits:
            blocks.setdefault("phone " + digits, []).append(pos)
        if emails is not None:
            e = str(emails.iloc[pos]).lower()
            # placeholder addresses like no@gmail.com must not block together
            if "@" in e and e not in (NA.lower(), "nan") and not e.startswith("no@"):
                blocks.setdefault("email " + e, []).append(pos)

    parent = list(range(len(df)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    matched_on = {}
    for key, rows in blocks.items():
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                name_a, name_b = str(df.iloc[a]["Full Name"]), str(df.iloc[b]["Full Name"])
                if name_a == NA or name_b == NA:
                    continue
                ca, cb = _canon_name(name_a, nicknames), _canon_name(name_b, nicknames)
                if ca == cb or difflib.SequenceMatcher(None, ca, cb).ratio() >= threshold:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[rb] = ra
                    matched_on.setdefault(min(a, b), key)

    clusters = {}
    for pos in range(len(df)):
        clusters.setdefault(find(pos), []).append(pos)

    groups, row_to_group = [], {}
    gid = 0
    for members in clusters.values():
        if len(members) < 2:
            continue
        gid += 1
        for m in members:
            row_to_group[m + 1] = gid
        groups.append({
            "group": gid,
            "rows": [m + 1 for m in members],
            "names": [str(df.iloc[m]["Full Name"]) for m in members],
            "matched_on": matched_on.get(min(members), "phone/email block"),
        })
    return groups, row_to_group


# ---------------------------------------------------------------- scoring

def score_leads(df, violations, row_to_group, cfg):
    weights = cfg.get("scoring", {}).get("weights", {})
    threshold = cfg.get("scoring", {}).get("review_threshold", 70)
    rules = cfg.get("rules", {})

    cols = [c for c in df.columns if c in weights]
    total_weight = sum(weights[c] for c in cols) or 1

    penalties_by_row = {}
    for v in violations:
        p = rules.get(v["rule"], {}).get("penalty", 5)
        penalties_by_row.setdefault(v["row"], []).append((v["rule"], p))
    dup_penalty = rules.get("duplicate_group", {}).get("penalty", 10)
    if rules.get("duplicate_group", {}).get("enabled", True):
        for row in row_to_group:
            penalties_by_row.setdefault(row, []).append(("duplicate_group", dup_penalty))

    scores = []
    for pos in range(len(df)):
        row = pos + 1
        filled = sum(weights[c] for c in cols if str(df.iloc[pos][c]) != NA)
        completeness = round(100.0 * filled / total_weight)
        row_pens = penalties_by_row.get(row, [])
        penalty = sum(p for _, p in row_pens)
        score = max(0, min(100, completeness - penalty))
        scores.append({
            "row": row,
            "name": str(df.iloc[pos].get("Full Name", NA)),
            "completeness": completeness,
            "penalty": penalty,
            "issues": sorted({r for r, _ in row_pens}),
            "score": score,
            "review": score < threshold,
        })
    return scores


# ---------------------------------------------------------------- orchestrator

def refine(df, report, schema, cfg=None):
    """Attach vertical, violations, corrections, dup groups, and scores to
    the report. Never touches the cleaned output columns."""
    if cfg is None:
        cfg = load_config()
    aux = report.get("aux_data")

    present = [c for c in df.columns if len(df) and not (df[c] == NA).all()]
    if aux is not None and hasattr(aux, "columns"):
        present += [c for c in aux.columns if not (aux[c] == NA).all()]
    report["vertical"] = detect_vertical(
        present + list(report.get("unmapped_headers", [])), cfg)

    violations = []
    corrections, email_violations = email_checks(aux, cfg)
    violations += email_violations
    violations += rule_checks(df, aux, cfg)
    if cfg.get("rules", {}).get("phone_invalid", {}).get("enabled", True):
        for idx in report.get("invalid_phone_rows", []):
            violations.append({"row": idx + 1, "rule": "phone_invalid",
                               "fields": ["Phone Number"],
                               "message": "phone number is not 10 digits"})

    groups, row_to_group = find_duplicate_groups(df, aux, cfg)
    scores = score_leads(df, violations, row_to_group, cfg)

    report["violations"] = sorted(violations, key=lambda v: (v["row"], v["rule"]))
    report["email_corrections"] = corrections
    report["duplicate_groups"] = groups
    report["scores"] = scores
    report["review_rows"] = [s["row"] for s in scores if s["review"]]
    report["avg_score"] = round(sum(s["score"] for s in scores) / len(scores)) if scores else 0
    return report

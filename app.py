"""Leads Sorter CRM portal — upload a messy leads CSV, get an organized one back."""

import json
import os
import uuid

import pandas as pd
from flask import Flask, abort, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from organizer import NA, load_schema, organize_file
from pretty_export import build_pretty_workbook

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(BASE, "uploads")
OUTPUT = os.path.join(BASE, "output")
SCHEMAS = os.path.join(BASE, "schemas")

_CONFIG_DEFAULTS = {"host": "0.0.0.0", "port": 8080, "passphrase": "",
                    "retention_hours": 24, "max_upload_mb": 50}


def load_server_config():
    cfg = dict(_CONFIG_DEFAULTS)
    path = os.path.join(BASE, "server_config.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


SERVER_CONFIG = load_server_config()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(SERVER_CONFIG["max_upload_mb"]) * 1024 * 1024


@app.errorhandler(413)
def too_large(_e):
    limit = SERVER_CONFIG["max_upload_mb"]
    return render_template("index.html", result=None,
                           error=f"File is too large — the limit is {limit} MB. "
                                 "Ask the host to raise max_upload_mb in "
                                 "server_config.json if you need more."), 413


def _schema_path(name):
    safe = secure_filename(name) or "default"
    if not safe.endswith(".json"):
        safe += ".json"
    return os.path.join(SCHEMAS, safe)


@app.context_processor
def _inject_flags():
    return {"needs_passphrase": bool(SERVER_CONFIG.get("passphrase"))}


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", result=None, error=None)


@app.route("/organize", methods=["GET"])
def organize_get():
    # reaching here means a page refresh/back-nav on the results page, not a
    # real upload (the form only ever POSTs here) — send them back to start
    return redirect(url_for("index"))


@app.route("/organize", methods=["POST"])
def organize():
    import hmac
    required = str(SERVER_CONFIG.get("passphrase") or "")
    if required:
        given = str(request.form.get("passphrase") or "")
        if not hmac.compare_digest(given, required):
            return render_template("index.html", result=None,
                                   error="Wrong or missing passphrase."), 403
    file = request.files.get("file")
    if file is None or file.filename == "":
        return render_template("index.html", result=None,
                               error="Please choose a CSV or Excel file first.")

    schema_name = request.form.get("schema", "default")
    schema_file = _schema_path(schema_name)
    if not os.path.exists(schema_file):
        return render_template("index.html", result=None, error=f"Unknown schema: {schema_name}")
    schema = load_schema(schema_file)

    os.makedirs(UPLOADS, exist_ok=True)
    os.makedirs(OUTPUT, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    in_path = os.path.join(UPLOADS, token + "_" + (secure_filename(file.filename) or "upload.csv"))
    file.save(in_path)

    try:
        df, report = organize_file(in_path, schema)
    except Exception as exc:  # malformed file the parser can't recover from
        return render_template("index.html", result=None,
                               error=f"Could not read that file as CSV: {exc}")

    if report["output_rows"] == 0 and report["input_rows"] > 0:
        return render_template(
            "index.html", result=None,
            error="No lead data could be recognized in that file — none of its "
                  "columns matched the schema and nothing could be inferred from "
                  "the values. Check that this is a leads CSV.")

    # the file parsed and produced rows, but most of them are missing the
    # two fields every real lead has — this usually means the file's layout
    # wasn't actually recognized and the organizer fell back to guessing
    na_name = int((df["Full Name"] == NA).sum()) if "Full Name" in df.columns else 0
    na_phone = int((df["Phone Number"] == NA).sum()) if "Phone Number" in df.columns else 0
    report["low_confidence"] = report["output_rows"] > 0 and (
        na_name / report["output_rows"] > 0.5 or na_phone / report["output_rows"] > 0.5)

    out_path = os.path.join(OUTPUT, f"organized_{token}.csv")
    df.to_csv(out_path, index=False)

    # per-row diff report (every change the organizer made)
    diff_df = pd.DataFrame(report.get("row_diffs", []),
                           columns=["row", "field", "original", "new", "reason"])
    diff_df.to_csv(os.path.join(OUTPUT, f"diff_{token}.csv"), index=False)

    # separate error log: violations, dedup groups, scores, unmapped headers
    error_log = {k: report.get(k) for k in (
        "vertical", "avg_score", "scores", "review_rows", "violations",
        "email_corrections", "duplicate_groups", "removed_duplicates",
        "duplicates_removed", "invalid_phone_rows", "unmapped_headers",
        "dropped_headers", "skipped_non_lead_rows", "header_inferred",
        "input_rows", "output_rows", "low_confidence") if k in report}
    with open(os.path.join(OUTPUT, f"errors_{token}.json"), "w", encoding="utf-8") as f:
        json.dump(error_log, f, indent=2)

    scores_by_row = {s["row"]: s for s in report.get("scores", [])}
    invalid = set(report["invalid_phone_rows"])
    rows = []
    for pos, cells in enumerate(df.values.tolist()):
        s = scores_by_row.get(pos + 1, {})
        rows.append({"cells": cells, "score": s.get("score", ""),
                     "review": s.get("review", False), "invalid": pos in invalid})

    build_pretty_workbook(list(df.columns), rows, os.path.join(OUTPUT, f"pretty_{token}.xlsx"))

    result = {
        "columns": list(df.columns),
        "rows": rows,
        "report": report,
        "token": token,
    }
    return render_template("index.html", result=result, error=None)


def _serve(token, pattern, download_name):
    if not token.isalnum():
        abort(404)
    path = os.path.join(OUTPUT, pattern.format(token=token))
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=download_name)


@app.route("/download/<token>")
def download(token):
    return _serve(token, "organized_{token}.csv", "organized_leads.csv")


@app.route("/xlsx/<token>")
def download_xlsx(token):
    return _serve(token, "pretty_{token}.xlsx", "organized_leads.xlsx")


@app.route("/diff/<token>")
def download_diff(token):
    return _serve(token, "diff_{token}.csv", "change_report.csv")


@app.route("/errors/<token>")
def download_errors(token):
    return _serve(token, "errors_{token}.json", "error_log.json")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

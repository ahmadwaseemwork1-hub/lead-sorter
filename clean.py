"""CLI mode for the leads refinement pipeline.

Usage:
    python clean.py INPUT [-o OUTDIR] [--schema NAME]

Writes next to the input (or into OUTDIR):
    <stem>_organized.csv   cleaned leads (canonical 10-column layout)
    <stem>_changes.csv     per-cell change log (row, field, before, after, reason)
    <stem>_errors.json     violations, scores, duplicate groups, review list
"""

import argparse
import json
import os
import sys

import pandas as pd

from organizer import load_schema, organize_file

BASE = os.path.dirname(os.path.abspath(__file__))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Clean and organize a leads file.")
    ap.add_argument("input", help="CSV/TSV/XLSX/XLS leads file")
    ap.add_argument("-o", "--outdir", default=None, help="output directory")
    ap.add_argument("--schema", default="default", help="schema profile name")
    args = ap.parse_args(argv)

    if not os.path.exists(args.input):
        print(f"error: no such file: {args.input}", file=sys.stderr)
        return 2
    schema_path = os.path.join(BASE, "schemas", args.schema + ".json")
    if not os.path.exists(schema_path):
        print(f"error: unknown schema: {args.schema}", file=sys.stderr)
        return 2

    schema = load_schema(schema_path)
    df, report = organize_file(args.input, schema)

    outdir = args.outdir or os.path.dirname(os.path.abspath(args.input))
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.input))[0]

    cleaned = os.path.join(outdir, f"{stem}_organized.csv")
    df.to_csv(cleaned, index=False)

    changes = os.path.join(outdir, f"{stem}_changes.csv")
    pd.DataFrame(report.get("row_diffs", []),
                 columns=["row", "field", "original", "new", "reason"]
                 ).to_csv(changes, index=False)

    errors = os.path.join(outdir, f"{stem}_errors.json")
    log = {k: report.get(k) for k in (
        "vertical", "avg_score", "scores", "review_rows", "violations",
        "email_corrections", "duplicate_groups", "removed_duplicates",
        "duplicates_removed", "invalid_phone_rows", "unmapped_headers",
        "dropped_headers", "skipped_non_lead_rows", "header_inferred",
        "input_rows", "output_rows") if k in report}
    with open(errors, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)

    print(f"input rows:        {report['input_rows']}")
    print(f"clean leads out:   {report['output_rows']}")
    print(f"duplicates removed:{report['duplicates_removed']:>5}")
    print(f"avg quality score: {report.get('avg_score', '-')}")
    print(f"manual review:     {len(report.get('review_rows', []))}")
    print(f"violations:        {len(report.get('violations', []))}")
    print(f"changes logged:    {len(report.get('row_diffs', []))}")
    print()
    print(f"cleaned: {cleaned}")
    print(f"changes: {changes}")
    print(f"errors:  {errors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

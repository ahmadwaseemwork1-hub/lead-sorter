import io
import json
import os

from organizer import NA, load_schema, organize_file

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = load_schema(os.path.join(BASE, "schemas", "default.json"))
COLUMNS = [c["name"] for c in SCHEMA["columns"]]


def organize_text(text, tmp_path):
    p = tmp_path / "in.csv"
    p.write_text(text, encoding="utf-8")
    return organize_file(str(p), SCHEMA)


# R1. zero regressions: the known-good sample still produces byte-identical output
def test_snapshot_regression():
    df, _ = organize_file(os.path.join(BASE, "sample_data", "messy_leads.csv"), SCHEMA)
    with open(os.path.join(BASE, "tests", "fixtures", "expected_messy_leads.csv"),
              encoding="utf-8") as f:
        expected = f.read().replace("\r\n", "\n")
    assert df.to_csv(index=False).replace("\r\n", "\n") == expected
    assert list(df.columns) == COLUMNS  # score/email/etc never leak into the CSV


# R2. at least 10 header synonyms map to canonical fields
def test_ten_header_synonyms_mapped(tmp_path):
    csv = (
        "Created At,Lead Name,Birthdate,Home Address,Cell Phone,"
        "Postal Code,Own or Rent,Number of Autos,Insurance Company,Vehicle Details\n"
        '01/05/2026,SYN TESTCASE,02/03/1980,"9 Syn St, Atlanta GA",4045550400,'
        "30301,own,2,geico,2020 Kia Rio\n"
    )
    df, report = organize_text(csv, tmp_path)
    row = df.iloc[0]
    assert row["Timestamp"] == "01/05/2026"
    assert row["Full Name"] == "Syn Testcase"
    assert row["Date of Birth"] == "02/03/1980"
    assert row["Address"] == "9 Syn St, Atlanta GA"
    assert row["Phone Number"] == "(404) 555-0400"
    assert row["ZIP Code"] == "30301"
    assert row["Homeowner"] == "Owner"
    assert row["Autos"] == "2"
    assert row["Current Insurance"] == "GEICO"
    assert row["Cars Make and Model"] == "2020 Kia Rio"
    assert report["unmapped_headers"] == []


# R3. gmial.com -> gmail.com auto-corrected in aux data and logged; never in CSV
def test_email_domain_typo_corrected(tmp_path):
    csv = (
        "Timestamp,Full Name,Phone,Email\n"
        "01/05/2026,Typo Testmail,4045550410,typo.testmail@gmial.com\n"
        "01/05/2026,Fine Testmail,4045550411,fine@gmail.com\n"
        "01/05/2026,Bad Testmail,4045550412,not-an-email\n"
    )
    df, report = organize_text(csv, tmp_path)
    assert "Email" not in df.columns
    fixes = report["email_corrections"]
    assert len(fixes) == 1
    assert fixes[0]["original"] == "typo.testmail@gmial.com"
    assert fixes[0]["suggested"] == "typo.testmail@gmail.com"
    assert fixes[0]["applied"] is True
    assert report["aux_data"]["Email"].iloc[0] == "typo.testmail@gmail.com"
    rules = {v["rule"] for v in report["violations"]}
    assert "email_domain_typo" in rules and "email_invalid" in rules


# R4. Jon Smith / John Smith with the same phone: flagged as one dup group,
#     neither row deleted (names differ, so exact dedupe must not fire)
def test_nickname_fuzzy_dedup_flagged(tmp_path):
    csv = (
        "Timestamp,Full Name,Phone\n"
        "01/05/2026,Jon Smithtest,4045550420\n"
        "01/06/2026,John Smithtest,4045550420\n"
        "01/07/2026,Sally Unrelated,4045550421\n"
    )
    df, report = organize_text(csv, tmp_path)
    assert report["duplicates_removed"] == 0
    assert len(df) == 3
    groups = report["duplicate_groups"]
    assert len(groups) == 1
    assert groups[0]["rows"] == [1, 2]
    assert set(groups[0]["names"]) == {"Jon Smithtest", "John Smithtest"}


# R5. retired/student vs DOB conflict flagged, data untouched
def test_retired_student_dob_conflict(tmp_path):
    csv = (
        "Timestamp,Full Name,DOB,Phone,Occupation\n"
        "01/05/2026,Old Studenttest,01/01/1950,4045550430,Student\n"
        "01/05/2026,Young Retiredtest,01/01/2000,4045550431,Retired\n"
        "01/05/2026,Normal Workertest,01/01/1980,4045550432,Engineer\n"
    )
    df, report = organize_text(csv, tmp_path)
    hits = [v for v in report["violations"] if v["rule"] == "dob_vs_occupation"]
    assert {v["row"] for v in hits} == {1, 2}
    assert df.iloc[0]["Date of Birth"] == "01/01/1950"  # never mutated


# R6. explicit Age column contradicting DOB flagged
def test_dob_vs_age_mismatch(tmp_path):
    csv = (
        "Timestamp,Full Name,DOB,Phone,Age\n"
        "01/05/2026,Age Mismatchtest,01/01/1990,4045550440,20\n"
        "01/05/2026,Age Finetest,01/01/1990,4045550441,36\n"
    )
    df, report = organize_text(csv, tmp_path)
    hits = [v for v in report["violations"] if v["rule"] == "dob_vs_age"]
    assert [v["row"] for v in hits] == [1]


# R7. scoring: complete lead passes, sparse lead lands in manual review
def test_scores_and_review_bucket(tmp_path):
    csv = (
        "Timestamp,Full Name,DOB,Address,Phone,Zip,Homeowner,Autos,Current Ins,Make and Model\n"
        '01/05/2026,Full Leadtest,02/03/1980,"1 Full St, Atlanta GA",4045550450,'
        "30301,own,2,geico,2020 Kia Rio\n"
        "01/05/2026,Sparse Leadtest,,,,,,,,\n"
    )
    df, report = organize_text(csv, tmp_path)
    scores = {s["name"]: s for s in report["scores"]}
    assert scores["Full Leadtest"]["score"] >= 70
    assert scores["Full Leadtest"]["review"] is False
    assert scores["Sparse Leadtest"]["score"] < 70
    assert scores["Sparse Leadtest"]["review"] is True
    assert scores["Sparse Leadtest"]["row"] in report["review_rows"]


# R8. every change appears in the per-row diff with a reason
def test_row_diff_report(tmp_path):
    csv = (
        "Timestamp,Full Name,Phone,Current Ins\n"
        "01/05/2026,DIFF TESTCASE,404-555-0460,gieco\n"
    )
    df, report = organize_text(csv, tmp_path)
    diffs = {(d["field"]): d for d in report["row_diffs"] if d["row"] == 1}
    assert diffs["Full Name"]["original"] == "DIFF TESTCASE"
    assert diffs["Full Name"]["new"] == "Diff Testcase"
    assert diffs["Phone Number"]["new"] == "(404) 555-0460"
    assert diffs["Current Insurance"]["original"] == "gieco"
    assert diffs["Current Insurance"]["new"] == "GEICO"
    assert all(d["reason"] for d in report["row_diffs"])


# R9. mixed/messy orientation: junk title line, blank lines, header not on
#     line 1, stray note row, empty cells — parsed correctly
def test_mixed_orientation_junk_file(tmp_path):
    csv = (
        "LEADS EXPORT JULY,,,,\n"
        ",,,,\n"
        "Timestamp,Full Name,Phone,Email,Age\n"
        "01/05/2026,MIXED TESTCASE,4045550470,mixed@gmail.com,\n"
        ",,,,\n"
        "note to self,,,,\n"
        "01/06/2026,Second Mixedtest,4045550471,second@yahoo.com,44\n"
    )
    df, report = organize_text(csv, tmp_path)
    assert df["Full Name"].tolist() == ["Mixed Testcase", "Second Mixedtest"]
    assert report["header_inferred"] is False
    assert report["empty_rows_skipped"] == 1  # the "note to self" row
    assert report["skipped_non_lead_rows"] >= 1  # junk title above the header


# R11. split fields composed: First+Last Name -> Full Name,
#      Street+City+State -> Address (headered files)
def test_split_name_and_address_composed(tmp_path):
    csv = (
        "Created,First Name,Last Name,DOB,Phone,Street,City,State,ZIP\n"
        "2026-01-05 10:30,ETHAN,COMPOSETEST,06/12/1990,4045550500,"
        "8679 Oak St,Aurora,CO,80633\n"
    )
    df, report = organize_text(csv, tmp_path)
    row = df.iloc[0]
    assert row["Full Name"] == "Ethan Composetest"
    assert row["Address"] == "8679 Oak St, Aurora, CO"
    assert row["ZIP Code"] == "80633"
    assert row["Timestamp"] == "01/05/2026 10:30"
    assert "First Name" not in report["unmapped_headers"]
    assert "Street" not in report["unmapped_headers"]


# R12. Excel (.xlsx) input works end to end
def test_xlsx_input(tmp_path):
    import pandas as pd
    p = tmp_path / "leads.xlsx"
    pd.DataFrame({
        "First Name": ["Xl", "Second"],
        "Last Name": ["Sheettest", "Sheettest"],
        "DOB": ["01/02/1980", "03/04/1975"],
        "Phone": ["4045550510", "4045550511"],
        "Email": ["xl.sheettest@gmial.com", "ok@yahoo.com"],
        "Street": ["1 Excel Way", "2 Excel Way"],
        "City": ["Denver", "Aurora"],
        "State": ["CO", "CO"],
        "ZIP": ["80202", "80011"],
    }).to_excel(p, index=False)
    df, report = organize_file(str(p), SCHEMA)
    assert len(df) == 2
    assert df.iloc[0]["Full Name"] == "Xl Sheettest"
    assert df.iloc[0]["Address"] == "1 Excel Way, Denver, CO"
    assert df.iloc[0]["Phone Number"] == "(404) 555-0510"
    fixes = report["email_corrections"]
    assert len(fixes) == 1 and fixes[0]["suggested"] == "xl.sheettest@gmail.com"


# R13. content-based type detection: an XLSX renamed to .csv still parses
def test_xlsx_disguised_as_csv(tmp_path):
    import pandas as pd
    real = tmp_path / "real.xlsx"
    pd.DataFrame({"Full Name": ["Disguise Testcase"], "Phone": ["4045550520"],
                  "Timestamp": ["01/05/2026"]}).to_excel(real, index=False)
    fake = tmp_path / "renamed.csv"
    fake.write_bytes(real.read_bytes())
    df, _ = organize_file(str(fake), SCHEMA)
    assert df.iloc[0]["Full Name"] == "Disguise Testcase"
    assert df.iloc[0]["Phone Number"] == "(404) 555-0520"


# R14. delimiter auto-detection: TSV and semicolon files parse
def test_tsv_and_semicolon_delimiters(tmp_path):
    tsv = tmp_path / "leads.tsv"
    tsv.write_text("Timestamp\tFull Name\tPhone\n01/05/2026\tTab Testcase\t4045550530\n",
                   encoding="utf-8")
    df, _ = organize_file(str(tsv), SCHEMA)
    assert df.iloc[0]["Full Name"] == "Tab Testcase"

    semi = tmp_path / "leads_semi.csv"
    semi.write_text("Timestamp;Full Name;Phone\n01/05/2026;Semi Testcase;4045550531\n",
                    encoding="utf-8")
    df2, _ = organize_file(str(semi), SCHEMA)
    assert df2.iloc[0]["Full Name"] == "Semi Testcase"


# R15. non-UTF8 encoding tolerated
def test_cp1252_encoding(tmp_path):
    p = tmp_path / "latin.csv"
    p.write_bytes("Timestamp,Full Name,Phone\n01/05/2026,Ren\xe9 Testcase,4045550540\n"
                  .encode("cp1252"))
    df, _ = organize_file(str(p), SCHEMA)
    assert "Testcase" in df.iloc[0]["Full Name"]
    assert df.iloc[0]["Phone Number"] == "(404) 555-0540"


# R16. CLI mode produces all three artifacts
def test_cli_mode(tmp_path):
    import clean
    src = tmp_path / "cli_in.csv"
    src.write_text("Timestamp,Full Name,Phone\n01/05/2026,CLI TESTCASE,4045550550\n",
                   encoding="utf-8")
    out = tmp_path / "out"
    rc = clean.main([str(src), "-o", str(out)])
    assert rc == 0
    assert (out / "cli_in_organized.csv").exists()
    assert (out / "cli_in_changes.csv").exists()
    assert (out / "cli_in_errors.json").exists()
    body = (out / "cli_in_organized.csv").read_text(encoding="utf-8")
    assert "Cli Testcase" in body


# R10. vertical detection: insurance columns -> insurance; generic -> general
def test_vertical_detection(tmp_path):
    ins = "Timestamp,Full Name,Phone,Current Ins,Autos\n01/05/2026,Vert Instest,4045550480,geico,2\n"
    _, rep1 = organize_text(ins, tmp_path)
    assert rep1["vertical"] == "insurance"
    gen = "Full Name,Phone\nVert Gentest,4045550481\n"
    p = tmp_path / "gen.csv"
    p.write_text(gen, encoding="utf-8")
    _, rep2 = organize_file(str(p), SCHEMA)
    assert rep2["vertical"] == "general"

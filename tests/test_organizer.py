import os

import pytest

from organizer import (
    NA,
    load_schema,
    normalize_carrier,
    normalize_date,
    normalize_homeowner,
    normalize_name,
    normalize_phone,
    normalize_zip,
    organize_file,
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = load_schema(os.path.join(BASE, "schemas", "default.json"))
COLUMNS = [c["name"] for c in SCHEMA["columns"]]


def organize_text(text, tmp_path):
    p = tmp_path / "in.csv"
    p.write_text(text, encoding="utf-8")
    return organize_file(str(p), SCHEMA)


# 1. header aliases from messy inputs map onto schema columns
def test_header_alias_mapping(tmp_path):
    csv = (
        "Time stamp,NAME,dob,addr,Phone,Zipcode,Own/Rent,vehicles,Carrier,car info\n"
        '01/05/2026,JANE TESTDOE,01/02/1990,"1 Test St, Atlanta GA",4045550199,30301,own,2,geico,2020 Honda Fit\n'
    )
    df, report = organize_text(csv, tmp_path)
    assert list(df.columns) == COLUMNS
    row = df.iloc[0]
    assert row["Full Name"] == "Jane Testdoe"
    assert row["Date of Birth"] == "01/02/1990"
    assert row["Phone Number"] == "(404) 555-0199"
    assert row["ZIP Code"] == "30301"
    assert row["Homeowner"] == "Owner"
    assert row["Autos"] == "2"
    assert row["Current Insurance"] == "GEICO"
    assert row["Cars Make and Model"] == "2020 Honda Fit"


# 2. ALL-CAPS and all-lowercase names become Title Case
def test_allcaps_name_titlecased():
    assert normalize_name("LARRY TESTHENDERSON") == "Larry Testhenderson"
    assert normalize_name("mary jane testsmith") == "Mary Jane Testsmith"
    assert normalize_name("Shideh McTestparsa") == "Shideh McTestparsa"  # mixed case kept
    assert normalize_name("  extra   spaces  ") == "Extra Spaces"


# 3. mixed date formats normalize to MM/DD/YYYY
def test_mixed_date_formats():
    assert normalize_date("1966-01-29") == "01/29/1966"
    assert normalize_date("4/6/1950") == "04/06/1950"
    assert normalize_date("June 30, 1988") == "06/30/1988"
    assert normalize_date("not a date") == NA


# 4. phone formats normalize to (XXX) XXX-XXXX
def test_phone_formats_normalized():
    assert normalize_phone("4045550101") == "(404) 555-0101"
    assert normalize_phone("(770) 555-0102") == "(770) 555-0102"
    assert normalize_phone("678.555.0107") == "(678) 555-0107"
    assert normalize_phone("1-404-555-0105") == "(404) 555-0105"  # leading country code


# 5. invalid phone lengths are kept as digits and flagged in the report
def test_invalid_phone_flagged(tmp_path):
    csv = (
        "Timestamp,Full Name,Phone Number\n"
        "01/05/2026,Al Testshort,55501\n"
        "01/05/2026,Bo Testok,4045550101\n"
    )
    df, report = organize_text(csv, tmp_path)
    flagged = df.loc[report["invalid_phone_rows"], "Phone Number"].tolist()
    assert flagged == ["55501"]
    assert "(404) 555-0101" not in flagged


# 6. ZIP codes: leading zeros preserved, short zips padded, ZIP+4 trimmed
def test_zip_normalization(tmp_path):
    assert normalize_zip("02116") == "02116"
    assert normalize_zip("2116") == "02116"
    assert normalize_zip("30016-1234") == "30016"
    assert normalize_zip("303491234") == "30349"
    assert normalize_zip("") == NA
    # leading zero survives the full file round-trip
    csv = "Timestamp,Full Name,Zip\n01/05/2026,Zed Testzip,02116\n"
    df, _ = organize_text(csv, tmp_path)
    assert df.iloc[0]["ZIP Code"] == "02116"


# 7. homeowner case/word variants collapse to Owner / Rented
def test_homeowner_variants():
    for v in ["Owner", "OWNER", "owner", "own", "homeowner", "yes"]:
        assert normalize_homeowner(v) == "Owner", v
    for v in ["Rented", "rented", "RENT", "renter", "tenant", "no"]:
        assert normalize_homeowner(v) == "Rented", v
    assert normalize_homeowner("banana") == NA


# 8. carrier misspellings map to canonical names
def test_carrier_misspellings():
    assert normalize_carrier("Gieco") == "GEICO"
    assert normalize_carrier("GIECO") == "GEICO"
    assert normalize_carrier("prograssive") == "Progressive"
    assert normalize_carrier("Progresive") == "Progressive"
    assert normalize_carrier("STATE FARM") == "State Farm"
    assert normalize_carrier("state farm") == "State Farm"
    assert normalize_carrier("Liberty") == "Liberty Mutual"
    assert normalize_carrier("usaa") == "USAA"
    # unknown carriers are kept, tidied
    assert normalize_carrier("ACME MUTUAL") == "Acme Mutual"


# 9. Agent Name and Leads Note columns are dropped, never in output
def test_agent_and_notes_dropped(tmp_path):
    csv = (
        "Timestamp,Full Name,Phone,Leads Note,Agent Name,Notes\n"
        "01/05/2026,Ana Testdrop,4045550111,hot lead,Leo,call back\n"
    )
    df, report = organize_text(csv, tmp_path)
    assert list(df.columns) == COLUMNS
    assert "Agent Name" not in df.columns and "Leads Note" not in df.columns
    assert set(report["dropped_headers"]) == {"Leads Note", "Agent Name", "Notes"}
    assert "Leo" not in df.to_csv(index=False)
    assert "hot lead" not in df.to_csv(index=False)


# 10. duplicates = same phone AND same name; different people sharing a phone
#     are both kept, NA phones never collapsed
def test_duplicate_leads_removed(tmp_path):
    csv = (
        "Timestamp,Full Name,Phone\n"
        "01/05/2026,Dupe Testperson,4045550120\n"
        "01/06/2026,DUPE TESTPERSON,(404) 555-0120\n"
        "01/07/2026,Spouse Testshared,4045550120\n"
        "01/08/2026,Nophone Testone,\n"
        "01/09/2026,Nophone Testtwo,\n"
    )
    df, report = organize_text(csv, tmp_path)
    assert report["duplicates_removed"] == 1
    assert len(df) == 4
    assert list(df["Full Name"]).count("Dupe Testperson") == 1
    assert "Spouse Testshared" in df["Full Name"].values  # shared phone, kept
    removed = report["removed_duplicates"]
    assert removed == [{"Full Name": "Dupe Testperson",
                        "Phone Number": "(404) 555-0120"}]


# 11. blank-line-riddled and fully empty files don't crash
def test_blank_lines_and_empty_file(tmp_path):
    csv = (
        "Timestamp,Full Name,Phone\n"
        "\n"
        "01/05/2026,Blank Testlines,4045550130\n"
        "\n\n"
        ",,\n"
        "01/06/2026,After Testblanks,4045550131\n"
    )
    df, report = organize_text(csv, tmp_path)
    assert len(df) == 2

    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    df2, report2 = organize_file(str(empty), SCHEMA)
    assert list(df2.columns) == COLUMNS
    assert len(df2) == 0 and report2["output_rows"] == 0


# 12. output column order is exactly the schema order
def test_output_column_order(tmp_path):
    csv = "Phone,Zip,Full Name,Timestamp\n4045550140,30301,Order Testcase,01/05/2026\n"
    df, _ = organize_text(csv, tmp_path)
    assert list(df.columns) == [
        "Timestamp", "Full Name", "Date of Birth", "Address", "Phone Number",
        "ZIP Code", "Homeowner", "Autos", "Current Insurance", "Cars Make and Model",
    ]


# 13a. default profile: leads keep the exact order of the uploaded file
def test_input_order_preserved(tmp_path):
    csv = (
        "Timestamp,Full Name,Phone\n"
        "03/01/2026,Zoe Testlate,4045550151\n"
        "01/01/2026,Bob Testearly,4045550152\n"
        "01/01/2026,Amy Testearly,4045550153\n"
    )
    df, _ = organize_text(csv, tmp_path)
    assert df["Full Name"].tolist() == ["Zoe Testlate", "Bob Testearly", "Amy Testearly"]


# 13b. sorting still works when a schema profile asks for it
def test_sorting_when_schema_requests_it(tmp_path):
    import copy
    sorted_schema = copy.deepcopy(SCHEMA)
    sorted_schema["sort_by"] = ["Timestamp", "Full Name"]
    p = tmp_path / "in.csv"
    p.write_text(
        "Timestamp,Full Name,Phone\n"
        "03/01/2026,Zoe Testlate,4045550151\n"
        "01/01/2026,Bob Testearly,4045550152\n"
        "01/01/2026,Amy Testearly,4045550153\n",
        encoding="utf-8",
    )
    df, _ = organize_file(str(p), sorted_schema)
    assert df["Full Name"].tolist() == ["Amy Testearly", "Bob Testearly", "Zoe Testlate"]


# 18. header row buried under junk rows (Google Sheets-style export) is found
def test_header_row_below_junk_rows(tmp_path):
    csv = (
        "Paused,,\n"
        "Table1,,\n"
        "Timestamp,Full Name,Phone\n"
        "01/05/2026,BURIED TESTHEADER,4045550160\n"
    )
    df, report = organize_text(csv, tmp_path)
    assert report["header_inferred"] is False
    assert len(df) == 1
    assert df.iloc[0]["Full Name"] == "Buried Testheader"
    assert df.iloc[0]["Phone Number"] == "(404) 555-0160"


# 19. headerless file (Voxpact-style): columns inferred from content,
#     split name/address/vehicles recombined, legend rows skipped
def test_headerless_file_columns_inferred(tmp_path):
    csv = (
        ",dead airs,,,,,,,,,,,,,,\n"
        ",Not Interested,,,,,,,,,,,,,,\n"
        "Jordan,Testhaynes,1/14/1997,NA,3035550201,3035550201,"
        "8012 W Fake Long Dr,Littleton,CO,80123,1,2017 TOYOTA COROLLA,15000,NA,NA,ROOT INSURANCE\n"
        "Arthur,Testwolter,2/4/1952,NA,3035550202,3035550202,"
        "4295 S Fake Penn St,Englewood,CO,80113,1,1996 SAAB 900,3000,NA,NA,State Farm\n"
        "MATTHEW,TESTALLENDER,12/19/1983,NA,3035550203,3035550203,"
        "7111 Fake Simms,Arvada,CO,80004,2,2010 JEEP WRANGLER,5000,1993 FORD ECONOLINE,1000,Progressive\n"
    )
    df, report = organize_text(csv, tmp_path)
    assert report["header_inferred"] is True
    assert len(df) == 3

    row = df[df["Full Name"] == "Matthew Testallender"].iloc[0]
    assert row["Date of Birth"] == "12/19/1983"
    assert row["Phone Number"] == "(303) 555-0203"
    assert row["ZIP Code"] == "80004"
    assert row["Autos"] == "2"
    assert row["Current Insurance"] == "Progressive"
    assert row["Address"] == "7111 Fake Simms, Arvada, CO"
    assert row["Cars Make and Model"] == "2010 JEEP WRANGLER / 1993 FORD ECONOLINE"

    row2 = df[df["Full Name"] == "Jordan Testhaynes"].iloc[0]
    assert row2["Current Insurance"] == "Root Insurance"  # unknown carrier kept
    assert row2["Cars Make and Model"] == "2017 TOYOTA COROLLA"


# 20. rows that normalize to nothing at all are skipped, never emitted as NA rows
def test_all_na_rows_skipped(tmp_path):
    csv = (
        "Timestamp,Full Name,Phone\n"
        "01/05/2026,Kept Testrow,4045550170\n"
        "junkdate,n/a,---\n"
    )
    df, report = organize_text(csv, tmp_path)
    assert len(df) == 1
    assert report["empty_rows_skipped"] == 1


# 21. grid-style file (one lead per COLUMN, labeled cells, banner rows):
#     flattened to one row per lead, blocks top-to-bottom, columns left-to-right
def test_grid_file_flattened(tmp_path):
    csv = (
        "Live Call Transfer :,Live Call Transfer :,Live Call Transfer :\n"
        "Name: JOHN GRIDTEST,Name : Mary Gridtest,Name: : Bob Gridtest\n"
        "DOB: 01/02/1960,DOB 03 /04/1971,DOB: 05;06/1982\n"
        "Number: 4045550301,Number : 4045550302,Number: 4045550303\n"
        "Email: no@gmail.com,Email: NA,Email: NA\n"
        "Address: 100 Test St,Address :,Address: 300 Test Ave\n"
        "City: Atlanta,200 Test Rd,City: = Macon\n"
        "State: GA,City: Savannah,State: Georgia\n"
        "Zip Code: 30301,State: GA,ZipCode:31201\n"
        "Information:,Zip Code: = 31401,Information:\n"
        "0 Accident,Information:,0 Accident\n"
        "homeowner: YES,0 Accident,Renter\n"
        "CAR: 1,Homeowner,CARS: 2\n"
        "2015 Toyota Camry,CAR: 1,2018 Ford F150\n"
        "Insurance: prograssive,2012 honda civic,2020 Kia Soul\n"
        "Lead Source: Unknown,STATEFARM 1 year,Inc: Gieco\n"
        ",,\n"
        "Live Call Transfer :,Live Call Transfer :,Live Call Transfer :\n"
        "Name: Second Blocktest,,Name: Third Blocktest\n"
        "DOB: 07/08/1955,,DOB: 09/10/1966\n"
        "Number: 4045550304,,Number: 4045550305\n"
        "Address: 400 Test Blvd,,Address: 500 Test Cir\n"
        "City: Athens,,City: Augusta\n"
        "Zip Code: 30601,,Zip Code: 30901\n"
        "Home Owner,,rent\n"
        "Farm Bureau 1 year,,Insurance: NATION WIDE\n"
    )
    df, report = organize_text(csv, tmp_path)
    assert report["header_inferred"] is True
    # order: block 1 left-to-right, then block 2 (middle column empty there)
    assert df["Full Name"].tolist() == [
        "John Gridtest", "Mary Gridtest", "Bob Gridtest",
        "Second Blocktest", "Third Blocktest",
    ]
    john, mary, bob, second, third = (df.iloc[i] for i in range(5))
    assert john["Date of Birth"] == "01/02/1960"
    assert john["Phone Number"] == "(404) 555-0301"
    assert john["Address"] == "100 Test St, Atlanta, GA"
    assert john["ZIP Code"] == "30301"
    assert john["Homeowner"] == "Owner"
    assert john["Autos"] == "1"
    assert john["Current Insurance"] == "Progressive"
    assert john["Cars Make and Model"] == "2015 Toyota Camry"
    # Mary: messy DOB, street in unlabeled cell under empty Address label
    assert mary["Date of Birth"] == "03/04/1971"
    assert mary["Address"] == "200 Test Rd, Savannah, GA"
    assert mary["ZIP Code"] == "31401"
    assert mary["Homeowner"] == "Owner"
    assert mary["Current Insurance"] == "State Farm"
    assert mary["Cars Make and Model"] == "2012 honda civic"
    # Bob: renter, ZipCode: variant, two cars, misspelled GEICO
    assert bob["Date of Birth"] == "05/06/1982"
    assert bob["Homeowner"] == "Rented"
    assert bob["ZIP Code"] == "31201"
    assert bob["Autos"] == "2"
    assert bob["Current Insurance"] == "GEICO"
    assert bob["Cars Make and Model"] == "2018 Ford F150 / 2020 Kia Soul"
    assert second["Current Insurance"] == "Farm Bureau"
    assert second["Homeowner"] == "Owner"
    assert third["Current Insurance"] == "Nationwide"
    assert third["Homeowner"] == "Rented"


# 14. the bundled sample file runs end-to-end through the organizer
def test_sample_file_end_to_end():
    sample = os.path.join(BASE, "sample_data", "messy_leads.csv")
    df, report = organize_file(sample, SCHEMA)
    assert list(df.columns) == COLUMNS
    assert report["input_rows"] == 17
    assert report["duplicates_removed"] == 1  # Ed Testwasson same name + phone
    assert report["output_rows"] == 16
    assert report["skipped_non_lead_rows"] == 2  # the two blank lines
    assert set(report["dropped_headers"]) == {"Leads Note", "Agent Name"}
    # no agent names or notes leak into the output
    blob = df.to_csv(index=False)
    for word in ["Leo", "Ethan", "Husnain", "hot lead", "follow up", "dup row"]:
        assert word not in blob
    # leading-zero zip survived
    assert "02116" in df["ZIP Code"].values
    # every non-NA phone is either valid format or flagged
    import re
    for i, v in df["Phone Number"].items():
        if v != NA and not re.fullmatch(r"\(\d{3}\) \d{3}-\d{4}", v):
            assert i in report["invalid_phone_rows"]

import csv
import io
import itertools
import os

import pytest

from organizer import (
    NA,
    assess_confidence,
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


# 22. linear card file (plain text, one label per LINE, leads separated by a
#     banner or an underscore rule — no delimiter at all, so it must be
#     detected before any CSV/delimiter sniffing runs on it)
def test_linear_card_file_flattened(tmp_path):
    txt = (
        "ITS JAKE FROM ALLSTATE\n\n"
        "Live Call Transfer :\n\n"
        "Name: Linda Lineartest\n"
        "DOB: 09/28/1937\n\n"
        "Number: 6787216991\n\n"
        "Email: NA\n\n"
        "Address: 32 Berkshire Dr\n"
        "City:      Cartersville\n"
        "State:    GA\n"
        "Zip Code:   30120\n\n"
        "Information:\n\n"
        "0 Accident\n0 Tickets\n\n"
        "Homeowner\n\n"
        "1 CAR\n2015 DODGE Journey\n\n"
        "Insurance:\nstate farm Ins. for more than 10 years.\n\n"
        "Lead Source: Unknown\n\n"
        "_________________________________________\n\n"
        "Live Call Transfer\n\n"
        "Name: Gladys Lineartest\n"
        "DOB: 08/08/1940\n\n"
        "Spouse: N/A\nDOB: N/A\n\n"
        "Number: 6783993458\n"
        "Email: N/A\n\n"
        "Address: 3276 Northside Pkwy NW\n"
        "City: Atlanta\n"
        "State: GA\n"
        "Zip Code: 30327\n\n"
        "Information:\n\n"
        "No Accidents\nNo Tickets\n\n"
        "Renter\n\n"
        "02 Car\n2014 GMC Sierra\n2016 Toyota Camry\n\n"
        "Insurance:\nProgressive Ins. about 2 years.\n\n"
        "Lead Source: Unknown\n"
        "________________________________________________________________________________\n\n"
        "Live Call Transfer\n\n"
        "Name: Third Lineartest\n"
        "DOB: 01/01/1980\n\n"
        "Number: 4045550301\n"
        "Address: 1 Test St\n"
        "City: Macon\nState: GA\nZip Code: 31201\n\n"
        "Homeowner\n1 CAR\n2010 Honda Civic\n\n"
        "Insurance:\nGeico Ins. about 1 year.\n\n"
        "Lead Source: Unknown\n"
        "________________________________________________________________________________\n\n"
        "Live Call Transfer\n\n"
        "Name: Fourth Lineartest\n"
        "DOB: 02/02/1985\n\n"
        "Number: 4045550302\n"
        "Address: 2 Test St\n"
        "City: Savannah\nState: GA\nZip Code: 31401\n\n"
        "Renter\n1 CAR\n2011 Ford Focus\n\n"
        "Insurance:\nAllstate Ins. about 1 year.\n\n"
        "Lead Source: Unknown\n"
        "________________________________________________________________________________\n"
    )
    df, report = organize_text(txt, tmp_path)
    assert report["header_inferred"] is True
    assert df["Full Name"].tolist() == [
        "Linda Lineartest", "Gladys Lineartest", "Third Lineartest", "Fourth Lineartest",
    ]
    linda, gladys = df.iloc[0], df.iloc[1]
    assert linda["Date of Birth"] == "09/28/1937"
    assert linda["Phone Number"] == "(678) 721-6991"
    assert linda["Address"] == "32 Berkshire Dr, Cartersville, GA"
    assert linda["ZIP Code"] == "30120"
    assert linda["Homeowner"] == "Owner"
    assert linda["Autos"] == "1"
    assert linda["Current Insurance"] == "State Farm"
    assert linda["Cars Make and Model"] == "2015 DODGE Journey"
    # Gladys: spouse's blank DOB line must not overwrite her own, two cars
    assert gladys["Date of Birth"] == "08/08/1940"
    assert gladys["Homeowner"] == "Rented"
    assert gladys["Autos"] == "2"
    assert gladys["Current Insurance"] == "Progressive"
    assert gladys["Cars Make and Model"] == "2014 GMC Sierra / 2016 Toyota Camry"


# 23. labeled-column file (CRM/quoting-tool export: each field is its OWN
#     row, label text repeated across every lead's column, actual value on
#     the row(s) below) — parsed per COLUMN as an independent token stream,
#     since columns drift out of row-alignment with each other and a single
#     column can stack more than one lead if "Contact Details" repeats down it
def test_labeled_columns_file_flattened(tmp_path):
    col0 = [  # two leads stacked in the same column, with violation noise
        # in between that must not leak into either lead's fields
        "Contact Details", "First Name", "John", "Last Name", "Testone",
        "Primary Phone", "4045550101", "Email", "john@test.com",
        "Address", "100 Test St", "City", "Atlanta", "State", "GA",
        "ZipCode", "30301", "Date of Birth", "01/02/1970",
        "Drivers", "Primary Driver", "Year", "2020", "Make", "Honda", "Model", "Accord",
        "State Farm Ins.",
        "Add Violations", "First Name", "John",
        "Contact Details", "First Name", "Jane", "Last Name", "Testtwo",
        "Primary Phone", "4045550102", "Email", "jane@test.com",
        "Address", "500 Test Way", "City", "Rome", "State", "GA",
        "ZipCode", "30165", "Date of Birth", "03/04/1975",
        "Drivers", "Primary Driver", "Year", "2019", "Make", "Toyota", "Model", "Camry",
        "Progressive Ins.",
    ]
    col1 = [  # a blank cell between a label and its value (real-world spacing)
        "Contact Details", "First Name", "Bob", "Last Name", "Testthree",
        "Primary Phone", "4045550103", "Email", "bob@test.com",
        "Address", "200 Test Ave", "City", "Macon", "State", "", "GA",
        "ZipCode", "31201", "Date of Birth", "05/06/1980",
        "Drivers", "Primary Driver", "Year", "2018", "Make", "Ford", "Model", "F150",
        "GEICO Ins.",
    ]
    col2 = [  # no vehicle at all for this lead
        "Contact Details", "First Name", "Alice", "Last Name", "Testfour",
        "Primary Phone", "4045550104", "Email", "alice@test.com",
        "Address", "300 Test Rd", "City", "Savannah", "State", "GA",
        "ZipCode", "31401", "Date of Birth", "07/08/1990",
        "Drivers",
    ]
    col3 = [
        "Contact Details", "First Name", "Carla", "Last Name", "Testfive",
        "Primary Phone", "4045550105", "Email", "carla@test.com",
        "Address", "400 Test Blvd", "City", "Athens", "State", "GA",
        "ZipCode", "30601", "Date of Birth", "09/10/1985",
        "Drivers",
    ]
    rows = itertools.zip_longest(col0, col1, col2, col3, fillvalue="")
    csv_text = "\n".join(",".join(row) for row in rows)

    df, report = organize_text(csv_text, tmp_path)
    assert report["header_inferred"] is True
    names = set(df["Full Name"].tolist())
    assert names == {
        "John Testone", "Jane Testtwo", "Bob Testthree", "Alice Testfour", "Carla Testfive",
    }

    john = df[df["Full Name"] == "John Testone"].iloc[0]
    assert john["Phone Number"] == "(404) 555-0101"
    assert john["Address"] == "100 Test St, Atlanta, GA"
    assert john["ZIP Code"] == "30301"
    assert john["Date of Birth"] == "01/02/1970"
    assert john["Current Insurance"] == "State Farm"
    assert john["Cars Make and Model"] == "2020 Honda Accord"

    # second lead stacked in the same column: its own fields, not John's
    jane = df[df["Full Name"] == "Jane Testtwo"].iloc[0]
    assert jane["Phone Number"] == "(404) 555-0102"
    assert jane["Date of Birth"] == "03/04/1975"
    assert jane["Current Insurance"] == "Progressive"
    assert jane["Cars Make and Model"] == "2019 Toyota Camry"

    bob = df[df["Full Name"] == "Bob Testthree"].iloc[0]
    assert bob["Address"] == "200 Test Ave, Macon, GA"  # blank cell skipped cleanly
    assert bob["Current Insurance"] == "GEICO"

    alice = df[df["Full Name"] == "Alice Testfour"].iloc[0]
    assert alice["Cars Make and Model"] == NA  # no vehicle data present, not fabricated


# 24. verifier/dialer dashboard scrape (UI chrome interleaved with real lead
#     data: "Refresh" button labels bound each lead card, the phone number
#     that triggered the transfer sits in a short preamble BEFORE "Refresh"
#     rather than inside the card, and an "Agent Info" section right after
#     each card repeats "Full Name" for the AGENT, not the lead)
def test_verifier_scrape_file_flattened(tmp_path):
    col_a = [
        "Phone Number", "4045550201", "No transfer found.", "Select Agent",
        "Refresh",
        "Full Name", "John Verifytest",
        "Date Of Birth", "01/02/1970",
        "Address", "100 Test St, Atlanta, FL 30301",
        "Select State", "9", "FL, Florida",
        "Spouse", "No", "No",
        "Auto & Home Info",
        "Current Auto Carrier", "porgressive more then 1 year",
        "Any Lapses", "No", "No",
        "Make And Model", "2020 Honda Accord", "2019 Toyota Camry",
        "Accidents", "No", "No",
        "Tickets", "No", "No",
        "Home Owner", "Yes", "Yes",
        "Transfer Lead Reset Form", "copyright noise",
        "Agent Info",
        "Full Name", "Some Agent",  # the AGENT's name — must not leak in
        "States", "Florida",
        "Verifier Notes", "some criteria noise",
        "Lead Info",
        "Phone Number", "4045550202", "No transfer found.", "Select Agent",
        "Refresh",
        "Full Name", "Jane Verifytwo",
        "Date Of Birth", "03/04/1975",
        "Address", "200 Test Ave, Macon, FL 31201",
        "Current Auto Carrier", "State farm - 2 years",
        "Make And Model", "2018 Ford F150",
        "Accidents", "No",
        "Home Owner", "No", "No",
    ]
    col_b = [
        "Phone Number", "4045550203",
        "Refresh",
        "Full Name", "Bob Verifythree",
        "Date Of Birth", "05/06/1980",
        "Address", "300 Test Rd, Savannah, FL 31401",
        "Current Auto Carrier", "Geico 10 years",
        "Home Owner", "Yes", "Yes",
        "Phone Number", "4045550204",
        "Refresh",
        "Full Name", "Alice Verifyfour",
        "Date Of Birth", "07/08/1990",
        "Address", "400 Test Blvd, Athens, FL 30601",
        "Home Owner", "No", "No",
    ]
    buf = io.StringIO()
    csv.writer(buf).writerows(itertools.zip_longest(col_a, col_b, fillvalue=""))
    csv_text = buf.getvalue()

    df, report = organize_text(csv_text, tmp_path)
    assert report["header_inferred"] is True
    names = set(df["Full Name"].tolist())
    assert names == {
        "John Verifytest", "Jane Verifytwo", "Bob Verifythree", "Alice Verifyfour",
    }
    assert "Some Agent" not in names  # agent's own name must not leak in as a lead

    john = df[df["Full Name"] == "John Verifytest"].iloc[0]
    assert john["Phone Number"] == "(404) 555-0201"  # from the preamble, not the card
    assert john["Date of Birth"] == "01/02/1970"
    assert john["Address"] == "100 Test St, Atlanta, FL 30301"
    assert john["ZIP Code"] == "30301"
    assert john["Homeowner"] == "Owner"
    assert john["Current Insurance"] == "Progressive"  # "porgressive" typo, duration stripped
    assert john["Cars Make and Model"] == "2020 Honda Accord / 2019 Toyota Camry"

    jane = df[df["Full Name"] == "Jane Verifytwo"].iloc[0]
    assert jane["Phone Number"] == "(404) 555-0202"
    assert jane["Homeowner"] == "Rented"
    assert jane["Current Insurance"] == "State Farm"
    assert jane["Cars Make and Model"] == "2018 Ford F150"

    bob = df[df["Full Name"] == "Bob Verifythree"].iloc[0]
    assert bob["Phone Number"] == "(404) 555-0203"
    assert bob["Current Insurance"] == "GEICO"
    assert bob["Homeowner"] == "Owner"

    alice = df[df["Full Name"] == "Alice Verifyfour"].iloc[0]
    assert alice["Phone Number"] == "(404) 555-0204"
    assert alice["Homeowner"] == "Rented"
    assert alice["Cars Make and Model"] == NA  # no vehicle data present, not fabricated


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


# 22. numbered "Vehicle N" columns are composed into Cars Make and Model,
#     and don't get fuzzy-mis-mapped onto Autos (a real regression found on
#     a real-world CRM export: "Vehicle 1" fuzzy-matched the "vehicles"
#     alias for Autos before "Vehicles in household" could claim it)
def test_numbered_vehicle_columns_composed(tmp_path):
    csv = (
        "First Name,Last Name,Phone,Vehicles in household,"
        "Vehicle 1,Vehicle 1 Miles,Vehicle 2,Vehicle 2 Miles\n"
        "Gwen,Kempin,7062120322,2,2012 Dodge Grand,6000,2013 Toyota Rav4,8000\n"
    )
    df, report = organize_text(csv, tmp_path)
    row = df.iloc[0]
    assert row["Autos"] == "2"  # from "Vehicles in household", not a vehicle year
    assert row["Cars Make and Model"] == "2012 Dodge Grand / 2013 Toyota Rav4"
    assert "Vehicle 1 Miles" in report["unmapped_headers"]
    assert "Vehicle 2 Miles" in report["unmapped_headers"]


# 23. a trailing "Insurance"/typo'd "Insurance" or "Co" doesn't block a
#     known carrier from being canonicalized
def test_carrier_noise_words_stripped():
    assert normalize_carrier("Safeco Insruance") == "Safeco"
    assert normalize_carrier("SAFECO INSURANCE") == "Safeco"
    assert normalize_carrier("Auto-Owners Insurance Co") == "Auto-Owners"


# 24. headerless ZIP recognized even when the spreadsheet stripped its
#     leading zero and added a thousands-separator comma (e.g. "6,790" for
#     06790) — but a comma-grouped, round-number mileage/price value in a
#     neighboring column must NOT be mistaken for one
def test_headerless_zip_comma_grouped(tmp_path):
    csv = (
        "Gina,Whaley,8603078402,57 Upper Valley Rd,Torrington,CT,\"6,790\","
        "3,2005 Honda Pilot,\"10,000\"\n"
        "Justin,Lawrence,2035191386,39 Northwood Dr,Waterbury,CT,\"6,708\","
        "1,2019 Subaru Impreza,\"5,000\"\n"
        "Joel,Cincron,8608901483,78 Highview Ave,New Britain,CT,\"6,053\","
        "1,2014 Jeep Cherokee,\"5,000\"\n"
    )
    df, report = organize_text(csv, tmp_path)
    assert set(df["ZIP Code"]) == {"06790", "06708", "06053"}


# 25. a near-constant alpha column (e.g. a repeated state abbreviation)
#     must not be mistaken for the Full Name column just because it scores
#     high on "looks like a word" — real names are far more diverse
def test_headerless_name_column_diversity_guard(tmp_path):
    firsts = ["Aaron", "Brianna", "Carlos", "Diana", "Evan", "Farah"]
    lasts = ["Testholt", "Testparks", "Testreyes", "Testwu", "Testli", "Testgomez"]
    rows = [
        f"CT,{firsts[i]},{lasts[i]},303555{i:04d},8010{i}"
        for i in range(6)
    ]
    csv = "\n".join(rows) + "\n"
    df, report = organize_text(csv, tmp_path)
    assert "Ct" not in df["Full Name"].values
    assert set(df["Full Name"]) == {f"{firsts[i]} {lasts[i]}" for i in range(6)}


# 26. assess_confidence flags a file whose layout wasn't really recognized
#     (even when it doesn't fail the plain missing-name/phone check), and
#     stays quiet for a normally-parsed file
def test_assess_confidence_flags_broken_layout(tmp_path):
    csv = "Address,Zip Code\n123 Test St,30301\n456 Test Ave,30302\n789 Test Rd,30303\n"
    df, _ = organize_text(csv, tmp_path)
    flag, reasons = assess_confidence(df)
    assert flag is True
    assert reasons


def test_assess_confidence_quiet_for_normal_file():
    sample = os.path.join(BASE, "sample_data", "messy_leads.csv")
    df, _ = organize_file(sample, SCHEMA)
    flag, reasons = assess_confidence(df)
    assert flag is False
    assert reasons == []


# 27. a real header row appearing deep in the file, right after a blank
#     row, means a second differently-shaped export was pasted below the
#     first block — each block should be organized under its own
#     header/inference pass instead of one layout being forced over both
def test_embedded_header_after_blank_row_splits_file(tmp_path):
    firsts = ["Aaron", "Brianna", "Carlos", "Diana", "Evan", "Farah",
              "Gabriel", "Holly", "Ian", "Jasmine", "Kevin", "Laura"]
    lasts = ["Testholt", "Testparks", "Testreyes", "Testwu", "Testli", "Testgomez",
             "Testnoor", "Testkane", "Testvo", "Testruiz", "Testking", "Testolsen"]
    headerless_rows = [
        f"{firsts[i]},{lasts[i]},303555{i:04d},Denver,CO,802{i:02d}"
        for i in range(12)
    ]
    csv = (
        "\n".join(headerless_rows) + "\n"
        "\n"
        "Name,Number,DOB,Address\n"
        "Zed Testzephyr,3035559999,1/1/1980,99 Zed St Denver CO 80299\n"
    )
    df, report = organize_text(csv, tmp_path)
    assert report["input_rows"] == 13  # 12 headerless leads + 1 headered lead
    assert len(df) == 13
    assert "Aaron Testholt" in df["Full Name"].values
    assert "Zed Testzephyr" in df["Full Name"].values
    zed = df[df["Full Name"] == "Zed Testzephyr"].iloc[0]
    assert zed["Phone Number"] == "(303) 555-9999"
    assert zed["Date of Birth"] == "01/01/1980"

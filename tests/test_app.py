import io
import os

import pytest

import app as app_module

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


# 15. portal round-trip: upload the messy sample, get table + downloadable CSV
def test_portal_upload_and_download(client):
    sample = os.path.join(BASE, "sample_data", "messy_leads.csv")
    with open(sample, "rb") as f:
        resp = client.post(
            "/organize",
            data={"file": (io.BytesIO(f.read()), "messy_leads.csv"), "schema": "default"},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Download organized CSV" in html
    # the summary may mention dropped headers, but no agent data may appear
    assert "<td>Leo</td>" not in html and "<th>Agent Name</th>" not in html
    assert "hot lead" not in html

    token = html.split("/download/")[1].split('"')[0]
    dl = client.get(f"/download/{token}")
    assert dl.status_code == 200
    body = dl.get_data(as_text=True)
    header = body.splitlines()[0]
    assert header == (
        "Timestamp,Full Name,Date of Birth,Address,Phone Number,"
        "ZIP Code,Homeowner,Autos,Current Insurance,Cars Make and Model"
    )
    assert "Leo" not in body and "hot lead" not in body


# 15b. diff report and error log are downloadable alongside the cleaned CSV
def test_diff_and_error_log_downloads(client):
    sample = os.path.join(BASE, "sample_data", "messy_leads.csv")
    with open(sample, "rb") as f:
        resp = client.post(
            "/organize",
            data={"file": (io.BytesIO(f.read()), "messy_leads.csv"), "schema": "default"},
            content_type="multipart/form-data",
        )
    token = resp.get_data(as_text=True).split("/download/")[1].split('"')[0]

    diff = client.get(f"/diff/{token}")
    assert diff.status_code == 200
    lines = diff.get_data(as_text=True).splitlines()
    assert lines[0] == "row,field,original,new,reason"
    assert len(lines) > 5  # the messy sample produces plenty of fixes

    import json
    errors = client.get(f"/errors/{token}")
    assert errors.status_code == 200
    log = json.loads(errors.get_data(as_text=True))
    assert "scores" in log and "violations" in log and "vertical" in log
    assert log["vertical"] == "insurance"
    assert len(log["scores"]) == 16


# 16. uploading nothing shows a friendly error, not a crash
def test_portal_no_file(client):
    resp = client.post("/organize", data={}, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert "Please choose a CSV or Excel file" in resp.get_data(as_text=True)


# 17. unknown download token 404s
def test_download_unknown_token(client):
    assert client.get("/download/doesnotexist000").status_code == 404


# 18. passphrase gate: wrong blocks upload, correct allows it
def test_passphrase_gate(client, monkeypatch):
    monkeypatch.setitem(app_module.SERVER_CONFIG, "passphrase", "team-secret")
    payload = b"Timestamp,Full Name,Phone\n01/05/2026,Gate Testcase,4045550560\n"

    resp = client.post("/organize", data={
        "file": (io.BytesIO(payload), "gate.csv"), "passphrase": "wrong"},
        content_type="multipart/form-data")
    assert resp.status_code == 403
    assert "passphrase" in resp.get_data(as_text=True).lower()

    resp2 = client.post("/organize", data={
        "file": (io.BytesIO(payload), "gate.csv"), "passphrase": "team-secret"},
        content_type="multipart/form-data")
    assert resp2.status_code == 200
    assert "Gate Testcase" in resp2.get_data(as_text=True)


# 19. oversized upload rejected with a clear message, not a crash
def test_upload_size_limit(client, monkeypatch):
    monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", 1024)
    big = b"Timestamp,Full Name,Phone\n" + b"x" * 5000
    resp = client.post("/organize", data={
        "file": (io.BytesIO(big), "big.csv")}, content_type="multipart/form-data")
    assert resp.status_code == 413
    assert "too large" in resp.get_data(as_text=True).lower()

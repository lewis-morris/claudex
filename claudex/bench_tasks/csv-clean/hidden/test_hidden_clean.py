import csv
import subprocess
import sys

from clean import clean_rows


def row(name="a", email="a@x.com", phone="07700900123", signed_up="2026-01-01"):
    return {"name": name, "email": email, "phone": phone, "signed_up": signed_up}


def test_trims_and_title_case():
    assert clean_rows([row(name="  aNNa smith ")])[0]["name"] == "Anna Smith"


def test_email_lower_and_invalid_dropped():
    out = clean_rows([row(email=" Anna@Example.COM "), row(email=""), row(email="x@@y.com"), row(email="nope")])
    assert [r["email"] for r in out] == ["anna@example.com"]


def test_phones():
    for raw in ("07700 900123", "+44 7700 900123", "0044 7700-900-123", "447700900123"):
        assert clean_rows([row(phone=raw)])[0]["phone"] == "+447700900123", raw
    assert clean_rows([row(phone="020 7946 0000")])[0]["phone"] == ""


def test_dates():
    for raw in ("2026-03-01", "01/03/2026", "1 Mar 2026"):
        assert clean_rows([row(signed_up=raw)])[0]["signed_up"] == "2026-03-01", raw
    assert clean_rows([row(signed_up="not a date")]) == []


def test_duplicates_keep_latest_and_sorted():
    out = clean_rows([row(email="b@x.com", name="old", signed_up="2026-01-01"),
                      row(email="B@x.com", name="new", signed_up="2026-02-01"), row(email="a@x.com")])
    assert [r["email"] for r in out] == ["a@x.com", "b@x.com"] and out[1]["name"] == "New"


def test_command_line(tmp_path):
    out = tmp_path / "out.csv"
    subprocess.run([sys.executable, "clean.py", "data/customers.csv", str(out)], check=True, timeout=60)
    rows = list(csv.DictReader(out.open()))
    assert [r["email"] for r in rows] == ["anna@example.com", "bob@example.com", "eve@example.com"]
    assert rows[0]["signed_up"] == "2026-04-01" and rows[0]["phone"] == "+447700900123"

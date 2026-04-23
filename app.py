import csv
import io
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from flask import Flask, redirect, render_template, request, url_for

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")
IMPORTYETI_BASE = "https://www.importyeti.com"
OPENCORPORATES_SEARCH_URL = "https://api.opencorporates.com/v0.4/companies/search"
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_PATTERN = re.compile(r"\+?\d[\d(). -]{6,}\d")
REQUEST_TIMEOUT_SECONDS = 20
MAX_CONTACT_VALUES = 5
MAX_OPENCORPORATES_RESULTS = 5
MIN_PHONE_DIGITS = 8

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(get_db()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shipments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                bill_number TEXT,
                shipper TEXT,
                consignee TEXT,
                importer TEXT,
                exporter TEXT,
                origin_port TEXT,
                destination_port TEXT,
                arrival_date TEXT,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                source TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                website TEXT,
                address TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )


def normalize_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def pick_field(row: dict[str, Any], names: list[str]) -> str:
    lowered = {k.lower().strip(): normalize_str(v) for k, v in row.items()}
    for name in names:
        for key, value in lowered.items():
            if name in key and value:
                return value
    return ""


def extract_contacts(text: str) -> tuple[list[str], list[str]]:
    if not text:
        return [], []
    emails = sorted(set(EMAIL_PATTERN.findall(text)))
    phones = sorted(set(PHONE_PATTERN.findall(text)))
    phones = [p for p in phones if len(re.sub(r"\D", "", p)) >= MIN_PHONE_DIGITS]
    return emails[:MAX_CONTACT_VALUES], phones[:MAX_CONTACT_VALUES]


def ingest_csv(file_bytes: bytes, source: str) -> int:
    decoded = file_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(decoded))
    now = datetime.now(timezone.utc).isoformat()
    count = 0

    with closing(get_db()) as conn, conn:
        for row in reader:
            shipper = pick_field(row, ["shipper", "supplier", "seller"])
            consignee = pick_field(row, ["consignee", "buyer"])
            importer = pick_field(row, ["importer"])
            exporter = pick_field(row, ["exporter"])
            bill_number = pick_field(row, ["bill", "bl_no", "bol"])
            origin_port = pick_field(row, ["origin", "load_port", "port_of_loading"])
            destination_port = pick_field(row, ["destination", "discharge", "port_of_discharge"])
            arrival_date = pick_field(row, ["arrival", "date"])

            row_text = json.dumps(row, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO shipments (
                    source, bill_number, shipper, consignee, importer, exporter,
                    origin_port, destination_port, arrival_date, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    bill_number,
                    shipper,
                    consignee,
                    importer,
                    exporter,
                    origin_port,
                    destination_port,
                    arrival_date,
                    row_text,
                    now,
                ),
            )

            emails, phones = extract_contacts(row_text)
            company_candidates = [c for c in [shipper, consignee, importer, exporter] if c]
            for company in company_candidates[:2]:
                conn.execute(
                    """
                    INSERT INTO contacts (company, source, email, phone, website, address, raw_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        company,
                        f"{source}-raw-row",
                        ", ".join(emails) if emails else None,
                        ", ".join(phones) if phones else None,
                        None,
                        None,
                        row_text,
                        now,
                    ),
                )

            count += 1
    return count


def upsert_contacts(company: str, source: str, records: list[dict[str, Any]]) -> int:
    if not records:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    with closing(get_db()) as conn, conn:
        for item in records:
            conn.execute(
                """
                INSERT INTO contacts (company, source, email, phone, website, address, raw_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company,
                    source,
                    normalize_str(item.get("email")) or None,
                    normalize_str(item.get("phone")) or None,
                    normalize_str(item.get("website")) or None,
                    normalize_str(item.get("address")) or None,
                    json.dumps(item, ensure_ascii=False),
                    now,
                ),
            )
            inserted += 1
    return inserted


def fetch_importyeti_contacts(company: str) -> list[dict[str, Any]]:
    query = quote_plus(company)
    url = f"{IMPORTYETI_BASE}/search?term={query}"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    emails, phones = extract_contacts(text)

    websites: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if href.startswith("http") and "importyeti.com" not in href:
            websites.append(href)
    websites = sorted(set(websites))[:MAX_CONTACT_VALUES]

    records = []
    if emails or phones or websites:
        records.append(
            {
                "email": ", ".join(emails) if emails else "",
                "phone": ", ".join(phones) if phones else "",
                "website": ", ".join(websites) if websites else "",
                "address": "",
                "notes": "Scraped from ImportYeti search page. Verify manually.",
            }
        )
    return records


def fetch_opencorporates_contacts(company: str) -> list[dict[str, Any]]:
    try:
        response = requests.get(
            OPENCORPORATES_SEARCH_URL,
            params={"q": company, "per_page": MAX_OPENCORPORATES_RESULTS},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        return []

    entries = payload.get("results", {}).get("companies", [])
    records = []
    for entry in entries:
        company_data = entry.get("company", {})
        website = normalize_str(company_data.get("registry_url"))
        address = normalize_str(company_data.get("registered_address_in_full"))
        records.append(
            {
                "email": "",
                "phone": "",
                "website": website,
                "address": address,
                "name": normalize_str(company_data.get("name")),
                "jurisdiction": normalize_str(company_data.get("jurisdiction_code")),
            }
        )
    return records


def find_shipments(company: str) -> list[sqlite3.Row]:
    pattern = f"%{company}%"
    with closing(get_db()) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM shipments
            WHERE shipper LIKE ? OR consignee LIKE ? OR importer LIKE ? OR exporter LIKE ?
            ORDER BY id DESC
            LIMIT 100
            """,
            (pattern, pattern, pattern, pattern),
        ).fetchall()
    return rows


def find_contacts(company: str) -> list[sqlite3.Row]:
    pattern = f"%{company}%"
    with closing(get_db()) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM contacts
            WHERE company LIKE ?
            ORDER BY id DESC
            LIMIT 100
            """,
            (pattern,),
        ).fetchall()
    return rows


@app.route("/", methods=["GET"])
def index():
    q = normalize_str(request.args.get("q", ""))
    shipments = find_shipments(q) if q else []
    contacts = find_contacts(q) if q else []
    return render_template("index.html", query=q, shipments=shipments, contacts=contacts)


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    source = normalize_str(request.form.get("source", "oec-bulk"))
    if not file or not file.filename:
        return redirect(url_for("index"))
    inserted = ingest_csv(file.read(), source=source)
    return redirect(url_for("index", q="", uploaded=inserted))


@app.route("/enrich", methods=["POST"])
def enrich():
    company = normalize_str(request.form.get("company", ""))
    if not company:
        return redirect(url_for("index"))

    importyeti_records = fetch_importyeti_contacts(company)
    opencorp_records = fetch_opencorporates_contacts(company)
    upsert_contacts(company, "importyeti", importyeti_records)
    upsert_contacts(company, "opencorporates", opencorp_records)

    return redirect(url_for("index", q=company))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080, debug=False)

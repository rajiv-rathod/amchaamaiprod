import csv
import io
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, redirect, render_template, request, url_for

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")
IMPORTYETI_BASE = "https://www.importyeti.com"
IMPORTYETI_DOMAIN = "importyeti.com"
OPENCORPORATES_SEARCH_URL = "https://api.opencorporates.com/v0.4/companies/search"
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_PATTERN = re.compile(r"\+?\d[\d(). -]{6,}\d")
REQUEST_TIMEOUT_SECONDS = 25
MAX_CONTACT_VALUES = 5
MAX_OPENCORPORATES_RESULTS = 5
MIN_PHONE_DIGITS = 8
MAX_COMPANIES_PER_ROW = 4
MAX_FETCH_RESULTS = 60
RECENT_LIMIT = 25
# Limits used inside the JSON-tree shipment finder
_JSON_MAX_DEPTH = 8      # maximum nesting depth to recurse
_JSON_SAMPLE_ITEMS = 5  # items to sample when scanning a list for shipments
_MIN_SCRIPT_BYTES = 200  # minimum <script> text length worth parsing as JSON
_MAX_IY_SLUGS_TO_TRY = 3  # number of ImportYeti search results to attempt
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Keys that indicate a dict is a shipment record (used in JSON tree search)
SHIPMENT_KEYS: frozenset[str] = frozenset(
    {
        "shipper",
        "consignee",
        "hs_code",
        "harmonized",
        "harmonized_code",
        "bill_of_lading",
        "bol",
        "arrival_date",
        "port_of_loading",
        "port_of_discharge",
        "discharge_port",
        "loading_port",
        "importer",
        "exporter",
        "weight",
        "quantity",
        "container",
        "shipper_name",
        "consignee_name",
    }
)

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
                hs_code TEXT,
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
                role TEXT,
                source TEXT NOT NULL,
                bill_number TEXT,
                hs_code TEXT,
                email TEXT,
                phone TEXT,
                website TEXT,
                address TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        ensure_column(conn, "shipments", "hs_code", "TEXT")
        ensure_column(conn, "contacts", "role", "TEXT")
        ensure_column(conn, "contacts", "bill_number", "TEXT")
        ensure_column(conn, "contacts", "hs_code", "TEXT")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {col["name"] for col in columns}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def normalize_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def to_like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped}%"


def pick_field(row: dict[str, Any], names: list[str]) -> str:
    lowered = {k.lower().strip(): normalize_str(v) for k, v in row.items()}
    for name in names:
        for key, value in lowered.items():
            if name in key and value:
                return value
    return ""


def pick_field_with_keywords(row: dict[str, Any], required: list[str], any_of: list[str]) -> str:
    lowered = {k.lower().strip(): normalize_str(v) for k, v in row.items()}
    for key, value in lowered.items():
        if not value:
            continue
        if all(token in key for token in required) and any(token in key for token in any_of):
            return value
    return ""


def role_contact_values(row: dict[str, Any], role: str) -> tuple[str, str]:
    if role == "sender":
        role_keys = ["shipper", "sender", "supplier", "seller", "exporter"]
    elif role == "receiver":
        role_keys = ["consignee", "receiver", "buyer", "importer"]
    elif role == "importer":
        role_keys = ["importer"]
    else:
        role_keys = ["exporter", "shipper"]

    email = pick_field_with_keywords(row, ["email"], role_keys)
    phone = pick_field_with_keywords(row, ["phone"], role_keys)
    if not phone:
        phone = pick_field_with_keywords(row, ["mobile"], role_keys)
    if not phone:
        phone = pick_field_with_keywords(row, ["tel"], role_keys)
    return email, phone


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def get_db_stats() -> dict[str, Any]:
    """Return counts for the dashboard header."""
    with closing(get_db()) as conn:
        shipments_count = conn.execute("SELECT COUNT(*) AS c FROM shipments").fetchone()["c"]
        contacts_count = conn.execute("SELECT COUNT(*) AS c FROM contacts").fetchone()["c"]
        companies_count = conn.execute(
            "SELECT COUNT(DISTINCT company) AS c FROM contacts"
        ).fetchone()["c"]
        sources = conn.execute(
            "SELECT source, COUNT(*) AS cnt FROM shipments GROUP BY source ORDER BY cnt DESC LIMIT 5"
        ).fetchall()
    return {
        "shipments": shipments_count,
        "contacts": contacts_count,
        "companies": companies_count,
        "sources": [{"source": r["source"], "count": r["cnt"]} for r in sources],
    }


def get_recent_shipments(limit: int = RECENT_LIMIT) -> list[sqlite3.Row]:
    with closing(get_db()) as conn:
        return conn.execute(
            "SELECT * FROM shipments ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


# ---------------------------------------------------------------------------
# Contact / email / phone helpers
# ---------------------------------------------------------------------------


def extract_contacts(text: str) -> tuple[list[str], list[str]]:
    if not text:
        return [], []
    emails = sorted(set(EMAIL_PATTERN.findall(text)))
    phones = sorted(set(PHONE_PATTERN.findall(text)))
    phones = [p for p in phones if len(re.sub(r"\D", "", p)) >= MIN_PHONE_DIGITS]
    return emails[:MAX_CONTACT_VALUES], phones[:MAX_CONTACT_VALUES]


# ---------------------------------------------------------------------------
# Auto-fetch from ImportYeti (free, public US import data)
# ---------------------------------------------------------------------------


def _find_shipment_arrays(obj: Any, depth: int = 0) -> list[list]:
    """
    Recursively walk a parsed-JSON structure looking for arrays whose first
    element is a dict whose keys overlap with known shipment field names.
    Returns the first matching list found (depth-first).
    """
    if depth > _JSON_MAX_DEPTH:
        return []
    if isinstance(obj, list) and obj:
        sample = obj[0]
        if isinstance(sample, dict):
            keys = {k.lower().replace(" ", "_").replace("-", "_") for k in sample}
            if keys & SHIPMENT_KEYS:
                return [obj]
        for item in obj[:5]:
            found = _find_shipment_arrays(item, depth + 1)
            if found:
                return found
    elif isinstance(obj, dict):
        for v in obj.values():
            found = _find_shipment_arrays(v, depth + 1)
            if found:
                return found
    return []


def _normalize_iy_shipment(raw: dict[str, Any], default_shipper: str = "") -> dict[str, Any]:
    """Map an ImportYeti raw record to our canonical shipment schema."""

    def g(*keys: str) -> str:
        for k in keys:
            canon = k.lower().replace("-", "_").replace(" ", "_")
            for rk, rv in raw.items():
                rk_canon = rk.lower().replace("-", "_").replace(" ", "_")
                if rk_canon == canon:
                    v = normalize_str(rv)
                    if v:
                        return v
        return ""

    return {
        "shipper": g("shipper_name", "shipper", "supplier_name", "supplier") or default_shipper,
        "consignee": g("consignee_name", "consignee", "buyer_name", "buyer"),
        "importer": g("importer_name", "importer"),
        "exporter": g("exporter_name", "exporter"),
        "hs_code": g("hs_code", "harmonized_code", "harmonized", "hs"),
        "bill_number": g("bill_of_lading", "bol_number", "bol", "bill"),
        "origin_port": g("port_of_loading", "loading_port", "origin_port", "origin", "pol"),
        "destination_port": g(
            "port_of_discharge", "discharge_port", "destination_port", "destination", "pod"
        ),
        "arrival_date": g("arrival_date", "arrival", "date", "estimated_arrival"),
        "description": g(
            "product_details", "product_description", "description", "commodity", "goods"
        ),
        "weight": g("weight", "gross_weight"),
        "quantity": g("quantity", "pieces", "cartons"),
    }


def _fetch_iy_company_page(slug: str, default_company: str) -> list[dict[str, Any]]:
    """
    Fetch one ImportYeti company page and extract shipment records.
    Tries three strategies in order:
      1. __NEXT_DATA__ JSON blob (Next.js SSR data)
      2. Any other <script> tag containing a JSON object
      3. HTML <table> fallback
    """
    url = f"{IMPORTYETI_BASE}/company/{slug}"
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        if not resp.ok:
            return []
    except requests.RequestException:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strategy 1: __NEXT_DATA__
    next_tag = soup.find("script", id="__NEXT_DATA__")
    if next_tag and next_tag.string:
        try:
            nd = json.loads(next_tag.string)
            arrays = _find_shipment_arrays(nd)
            if arrays:
                return [
                    _normalize_iy_shipment(item, default_company)
                    for item in arrays[0][:MAX_FETCH_RESULTS]
                    if isinstance(item, dict)
                ]
        except (json.JSONDecodeError, TypeError):
            pass

    # Strategy 2: any large <script> tag that is valid JSON
    for script in soup.find_all("script"):
        text = (script.string or "").strip()
        if len(text) < 200:
            continue
        try:
            data = json.loads(text)
            arrays = _find_shipment_arrays(data)
            if arrays:
                return [
                    _normalize_iy_shipment(item, default_company)
                    for item in arrays[0][:MAX_FETCH_RESULTS]
                    if isinstance(item, dict)
                ]
        except (json.JSONDecodeError, TypeError):
            pass

    # Strategy 3: HTML tables
    results: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        headers = [
            th.get_text(strip=True).lower().replace(" ", "_")
            for th in table.find_all("th")
        ]
        if not (set(headers) & SHIPMENT_KEYS):
            continue
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2:
                continue
            results.append(_normalize_iy_shipment(dict(zip(headers, cells)), default_company))
        if results:
            break

    return results[:MAX_FETCH_RESULTS]


def fetch_importyeti_shipments(company: str) -> list[dict[str, Any]]:
    """
    Main entry point: search ImportYeti for a company, find the correct slug,
    then fetch its shipment history page.
    """
    # Generate a slug from the company name and try it directly
    slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    records = _fetch_iy_company_page(slug, company)
    if records:
        return records

    # Search ImportYeti to find the canonical slug
    search_url = f"{IMPORTYETI_BASE}/search?term={quote_plus(company)}"
    tried_slugs: set[str] = {slug}
    try:
        resp = requests.get(search_url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        if not resp.ok:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")

        # Try __NEXT_DATA__ on search page for company list
        next_tag = soup.find("script", id="__NEXT_DATA__")
        if next_tag and next_tag.string:
            try:
                nd = json.loads(next_tag.string)
                page_props = nd.get("props", {}).get("pageProps", {})
                companies_list = (
                    page_props.get("companies")
                    or page_props.get("results")
                    or page_props.get("searchResults")
                    or []
                )
                if isinstance(companies_list, list):
                    for entry in companies_list[:3]:
                        if not isinstance(entry, dict):
                            continue
                        found_slug = normalize_str(
                            entry.get("slug")
                            or entry.get("urlSlug")
                            or re.sub(
                                r"[^a-z0-9]+",
                                "-",
                                normalize_str(entry.get("name", "")).lower(),
                            ).strip("-")
                        )
                        if found_slug and found_slug not in tried_slugs:
                            tried_slugs.add(found_slug)
                            records = _fetch_iy_company_page(found_slug, company)
                            if records:
                                return records
            except (json.JSONDecodeError, TypeError, KeyError):
                pass

        # HTML fallback: find /company/ links
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if "/company/" not in href:
                continue
            found_slug = href.split("/company/")[-1].strip("/").split("?")[0]
            if not found_slug or found_slug in tried_slugs:
                continue
            tried_slugs.add(found_slug)
            records = _fetch_iy_company_page(found_slug, company)
            if records:
                return records
            break  # only try the first result

    except requests.RequestException:
        pass

    return []


def ingest_fetched_shipments(
    records: list[dict[str, Any]], source: str, company: str = ""
) -> int:
    """Store auto-fetched shipment records and auto-create contact rows."""
    if not records:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    with closing(get_db()) as conn, conn:
        for rec in records:
            shipper = normalize_str(rec.get("shipper")) or company
            consignee = normalize_str(rec.get("consignee"))
            importer = normalize_str(rec.get("importer"))
            exporter = normalize_str(rec.get("exporter"))
            hs_code = normalize_str(rec.get("hs_code"))
            bill_number = normalize_str(rec.get("bill_number"))
            origin_port = normalize_str(rec.get("origin_port"))
            destination_port = normalize_str(rec.get("destination_port"))
            arrival_date = normalize_str(rec.get("arrival_date"))
            raw_text = json.dumps(rec, ensure_ascii=False)

            conn.execute(
                """
                INSERT INTO shipments (
                    source, bill_number, shipper, consignee, importer, exporter, hs_code,
                    origin_port, destination_port, arrival_date, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    bill_number or None,
                    shipper or None,
                    consignee or None,
                    importer or None,
                    exporter or None,
                    hs_code or None,
                    origin_port or None,
                    destination_port or None,
                    arrival_date or None,
                    raw_text,
                    now,
                ),
            )
            # Auto-create a contact row for each named party
            for role, name in [
                ("sender", shipper),
                ("receiver", consignee),
                ("importer", importer),
                ("exporter", exporter),
            ]:
                if not name:
                    continue
                conn.execute(
                    """
                    INSERT INTO contacts (
                        company, role, source, bill_number, hs_code,
                        email, phone, website, address, raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        role,
                        source,
                        bill_number or None,
                        hs_code or None,
                        None,
                        None,
                        None,
                        None,
                        raw_text,
                        now,
                    ),
                )
            count += 1
    return count


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
            hs_code = pick_field(row, ["hs_code", "hscode", "hs code", "harmonized"])
            bill_number = pick_field(row, ["bill", "bl_no", "bol"])
            origin_port = pick_field(row, ["origin", "load_port", "port_of_loading"])
            destination_port = pick_field(row, ["destination", "discharge", "port_of_discharge"])
            arrival_date = pick_field(row, ["arrival", "date"])

            row_text = json.dumps(row, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO shipments (
                    source, bill_number, shipper, consignee, importer, exporter, hs_code,
                    origin_port, destination_port, arrival_date, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    bill_number,
                    shipper,
                    consignee,
                    importer,
                    exporter,
                    hs_code,
                    origin_port,
                    destination_port,
                    arrival_date,
                    row_text,
                    now,
                ),
            )

            emails, phones = extract_contacts(row_text)
            role_company = [
                ("sender", shipper or exporter),
                ("receiver", consignee or importer),
                ("importer", importer),
                ("exporter", exporter),
            ]
            seen: set[tuple[str, str]] = set()
            for role, company in role_company[:MAX_COMPANIES_PER_ROW]:
                if not company:
                    continue
                key = (role, company)
                if key in seen:
                    continue
                seen.add(key)
                role_email, role_phone = role_contact_values(row, role)
                conn.execute(
                    """
                    INSERT INTO contacts (
                        company, role, source, bill_number, hs_code, email, phone, website, address, raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        company,
                        role,
                        f"{source}-raw-row",
                        bill_number or None,
                        hs_code or None,
                        role_email or (", ".join(emails) if emails else None),
                        role_phone or (", ".join(phones) if phones else None),
                        None,
                        None,
                        row_text,
                        now,
                    ),
                )

            count += 1
    return count


def upsert_contacts(
    company: str,
    source: str,
    records: list[dict[str, Any]],
    role: str = "other",
    hs_code: str = "",
    bill_number: str = "",
) -> int:
    if not records:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    with closing(get_db()) as conn, conn:
        for item in records:
            conn.execute(
                """
                INSERT INTO contacts (
                    company, role, source, bill_number, hs_code, email, phone, website, address, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company,
                    role,
                    source,
                    bill_number or None,
                    hs_code or None,
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
        response = requests.get(url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    emails, phones = extract_contacts(text)

    websites: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not href.startswith("http"):
            continue
        parsed = urlparse(href)
        if not parsed.netloc or not parsed.scheme.startswith("http"):
            continue
        host = parsed.netloc.lower()
        if host == IMPORTYETI_DOMAIN or host.endswith(f".{IMPORTYETI_DOMAIN}"):
            continue
        query_keys = [key.lower() for key in parse_qs(parsed.query, keep_blank_values=True).keys()]
        if any(key.startswith("utm_") or key in {"gclid", "fbclid"} for key in query_keys):
            continue
        websites.append(f"{parsed.scheme}://{parsed.netloc}{parsed.path or ''}")
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


def find_shipments(company: str, hs_code: str) -> list[sqlite3.Row]:
    clauses = []
    params: list[str] = []
    if company:
        pattern = to_like_pattern(company)
        clauses.append(
            "(shipper LIKE ? ESCAPE '\\' OR consignee LIKE ? ESCAPE '\\' OR importer LIKE ? ESCAPE '\\' OR exporter LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern, pattern])
    if hs_code:
        clauses.append("hs_code LIKE ? ESCAPE '\\'")
        params.append(to_like_pattern(hs_code))
    if not clauses:
        return []

    with closing(get_db()) as conn:
        sql = f"SELECT * FROM shipments WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 100"
        rows = conn.execute(sql, tuple(params)).fetchall()
    return rows


def find_contacts(company: str, hs_code: str, role: str) -> list[sqlite3.Row]:
    clauses = []
    params: list[str] = []
    if company:
        clauses.append("company LIKE ? ESCAPE '\\'")
        params.append(to_like_pattern(company))
    if hs_code:
        clauses.append("hs_code LIKE ? ESCAPE '\\'")
        params.append(to_like_pattern(hs_code))
    if role and role != "all":
        clauses.append("role = ?")
        params.append(role)
    if not clauses:
        return []

    with closing(get_db()) as conn:
        sql = f"SELECT * FROM contacts WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 100"
        rows = conn.execute(sql, tuple(params)).fetchall()
    return rows


@app.route("/", methods=["GET"])
def index():
    q = normalize_str(request.args.get("q", ""))
    hs = normalize_str(request.args.get("hs", ""))
    role = normalize_str(request.args.get("role", "all")) or "all"
    msg = normalize_str(request.args.get("msg", ""))
    shipments = find_shipments(q, hs) if (q or hs) else []
    contacts = find_contacts(q, hs, role) if (q or hs) else []
    recent = get_recent_shipments() if not (q or hs) else []
    stats = get_db_stats()
    return render_template(
        "index.html",
        query=q,
        hs=hs,
        selected_role=role,
        shipments=shipments,
        contacts=contacts,
        msg=msg,
        stats=stats,
        recent=recent,
    )


@app.route("/fetch", methods=["POST"])
def fetch_web():
    """Auto-fetch shipment data from free web sources for a company."""
    company = normalize_str(request.form.get("company", ""))
    hs = normalize_str(request.form.get("hs", ""))
    if not company:
        return redirect(url_for("index", msg="Please enter a company name to fetch."))

    iy_shipments = fetch_importyeti_shipments(company)
    total_s = ingest_fetched_shipments(iy_shipments, source="importyeti", company=company)

    iy_contacts = fetch_importyeti_contacts(company)
    oc_contacts = fetch_opencorporates_contacts(company)
    total_c = upsert_contacts(company, "importyeti", iy_contacts)
    total_c += upsert_contacts(company, "opencorporates", oc_contacts)

    if total_s or total_c:
        msg = (
            f"Fetched {total_s} shipment(s) and {total_c} contact record(s) "
            f"for \"{company}\" from free web sources."
        )
    else:
        msg = (
            f"No new data found for \"{company}\" on ImportYeti right now. "
            "Try a different spelling or a larger company name."
        )
    return redirect(url_for("index", q=company, hs=hs, msg=msg))


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    source = normalize_str(request.form.get("source", "oec-bulk"))
    if not file or not file.filename:
        return redirect(url_for("index"))
    inserted = ingest_csv(file.read(), source=source)
    return redirect(
        url_for("index", msg=f"Upload complete: {inserted} rows ingested.")
    )


@app.route("/enrich", methods=["POST"])
def enrich():
    company = normalize_str(request.form.get("company", ""))
    if not company:
        return redirect(url_for("index"))

    importyeti_records = fetch_importyeti_contacts(company)
    opencorp_records = fetch_opencorporates_contacts(company)
    upsert_contacts(company, "importyeti", importyeti_records, role="other")
    upsert_contacts(company, "opencorporates", opencorp_records, role="other")

    return redirect(url_for("index", q=company))


@app.route("/export", methods=["GET"])
def export_csv():
    """Download current contact search results as a CSV file."""
    q = normalize_str(request.args.get("q", ""))
    hs = normalize_str(request.args.get("hs", ""))
    role = normalize_str(request.args.get("role", "all")) or "all"
    contacts = find_contacts(q, hs, role) if (q or hs) else []

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["Role", "Company", "Bill No.", "HS/HX Code", "Source", "Email", "Phone", "Website", "Address"]
    )
    for row in contacts:
        writer.writerow(
            [
                row["role"] or "",
                row["company"],
                row["bill_number"] or "",
                row["hs_code"] or "",
                row["source"],
                row["email"] or "",
                row["phone"] or "",
                row["website"] or "",
                row["address"] or "",
            ]
        )

    filename = f"contacts-{q or 'all'}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080, debug=False)

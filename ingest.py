#!/usr/bin/env python3
"""
Ingest 3GPP Work Plan xlsx files into a SQLite database.

- Visits two 3GPP URLs, finds xlsx files matching filename patterns
- Downloads files not already present locally
- Iterates files in chronological order
- Skips files already completely processed (tracked in work_plan_files table)
- For each unprocessed file, reads rows, filters to work items only
  (nonzero unique_id), and inserts if not already in work_plan_items
"""

import os
import re
import sqlite3
import subprocess
from datetime import datetime, date
from pathlib import Path

import requests
from openpyxl import load_workbook
from dotenv import load_dotenv
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib
import ssl

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ── Config ──────────────────────────────────────────────────────────────

URLS = [
    "https://www.3gpp.org/ftp/information/work_plan/",
    "https://www.3gpp.org/ftp/information/work_plan/older_versions",
]

FILENAME_PATTERNS = [
    re.compile(r"Work_plan_3gpp_\d{6}.*?\.xlsx$", re.IGNORECASE),
    re.compile(r"Work_plan_3gpp_\d{4}_\d{2}_\d{2}.*?\.xlsx$", re.IGNORECASE),
]

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0"

XLSX_DIR = BASE_DIR / "xlsx"
DB_PATH = BASE_DIR / "threegpp.sqlite3"

# ── Schema ──────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS work_plan_files (
    source_file     TEXT PRIMARY KEY,
    file_date       TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    header_json     TEXT NOT NULL,
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS work_plan_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file     TEXT    NOT NULL,
    file_date       TEXT    NOT NULL,
    item_id         TEXT,
    unique_id       TEXT,
    name            TEXT    NOT NULL,
    acronym         TEXT,
    outline_level   INTEGER,
    release         TEXT,
    resource_names  TEXT,
    start_date      TEXT,
    finish_date     TEXT,
    completion      INTEGER,
    hyperlink       TEXT,
    status_report   TEXT,
    rapporteur_name TEXT,
    rapporteur_email TEXT,
    notes           TEXT,
    impacted_specs  TEXT,
    tsg_approved    TEXT,
    pcg_approved    TEXT,
    tsg_stopped     TEXT,
    pcg_stopped     TEXT,
    created         TEXT,
    last_modified   TEXT,
    content_hash    TEXT,                       -- hash of 21 compare cols for dedup
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_wpi_source_file   ON work_plan_items (source_file);
CREATE INDEX IF NOT EXISTS idx_wpi_item_id_file  ON work_plan_items (item_id, file_date);
CREATE INDEX IF NOT EXISTS idx_wpi_release_file  ON work_plan_items (release, file_date);
CREATE INDEX IF NOT EXISTS idx_wpi_unique_file   ON work_plan_items (unique_id, source_file);
CREATE INDEX IF NOT EXISTS idx_wpi_content_hash  ON work_plan_items (content_hash);
"""

# ── Column matching (from column_analysis.md) ───────────────────────────
#
# Two-tier: 9 exact-match invariant columns + 13 keyword-based variable columns.

INVARIANT_COLUMNS = {
    "id":            "id",
    "unique_id":     "unique_id",
    "name":          "name",
    "acronym":       "acronym",
    "release":       "release",
    "hyperlink":     "hyperlink",
    "notes":         "notes",
    "created":       "created",
    # "last_modif" with underscore (original/revert era) or space (rename era)
    "last_modif":    "last_modified",
    "last modif":    "last_modified",
}


def match_variable_column(header_lower: str) -> str | None:
    """Match a variable column header by keyword (lowercase)."""
    if "outline" in header_lower:
        return "outline_level"
    if "resource" in header_lower:
        return "resource_names"
    if "start" in header_lower:
        return "start_date"
    if "finish" in header_lower:
        return "finish_date"
    if "complet" in header_lower:  # percent_complete, %complete, completion
        return "completion"
    if "status" in header_lower or "summary" in header_lower:  # 161220 one-off
        return "status_report"
    if "rapporteur" in header_lower and "name" in header_lower:
        return "rapporteur_name"
    if "rapporteur" in header_lower and "mail" in header_lower:
        return "rapporteur_email"
    if "impacted" in header_lower:
        return "impacted_specs"
    if "tsg" in header_lower and "approv" in header_lower:
        return "tsg_approved"
    if "pcg" in header_lower and "approv" in header_lower:
        return "pcg_approved"
    if "tsg" in header_lower and "stop" in header_lower:
        return "tsg_stopped"
    if "pcg" in header_lower and "stop" in header_lower:
        return "pcg_stopped"
    return None


def build_column_map(headers: list[str]) -> dict[int, str]:
    """
    Given raw header strings, return {col_index: canonical_column_name}.
    Tier 1: exact match on 9 invariant columns.
    Tier 2: keyword match on 13 variable columns.
    """
    col_map = {}
    for i, h in enumerate(headers):
        hl = (h or "").strip().lower()
        # Tier 1: exact invariant match
        if hl in INVARIANT_COLUMNS:
            col_map[i] = INVARIANT_COLUMNS[hl]
            continue
        # Tier 2: keyword match
        matched = match_variable_column(hl)
        if matched:
            col_map[i] = matched
    return col_map


# ── Value normalization ─────────────────────────────────────────────────

SENTINELS = {"-", "na", "", "none"}


def is_sentinel(val) -> bool:
    if val is None:
        return True
    if isinstance(val, str) and val.strip().lower() in SENTINELS:
        return True
    return False


def normalize_int(val) -> int | None:
    if is_sentinel(val):
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return None


def normalize_text(val) -> str | None:
    if is_sentinel(val):
        return None
    return str(val).strip()


def normalize_timestamp(val) -> str | None:
    """Return ISO 8601 string or None."""
    if is_sentinel(val):
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    # String formats like "Thu 15/01/15" (dd/yy/mm)
    if isinstance(val, str):
        s = val.strip()
        # Try common formats
        for fmt in ["%a %d/%y/%m", "%d/%m/%Y", "%Y-%m-%d", "%d/%y/%m"]:
            try:
                return datetime.strptime(s, fmt).isoformat()
            except ValueError:
                continue
        return s  # store raw string if unparseable
    return None


def normalize_date(val) -> str | None:
    """Return ISO date string (YYYY-MM-DD) or None."""
    if is_sentinel(val):
        return None
    if isinstance(val, datetime):
        return val.date().isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, str):
        s = val.strip()
        for fmt in ["%a %d/%y/%m", "%d/%m/%Y", "%Y-%m-%d", "%d/%y/%m"]:
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue
        return s
    return None


# ── Filename parsing ────────────────────────────────────────────────────


def parse_file_date(filename: str) -> date:
    """Extract date from filename. Patterns: YYYY_MM_DD or YYMMDD."""
    m = re.search(r"(\d{4})_(\d{2})_(\d{2})", filename)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"_(\d{6})[a-z]*[_\.]", filename, re.IGNORECASE)
    if m:
        s = m.group(1)
        yy = int(s[:2])
        year = 2000 + yy if yy < 80 else 1900 + yy
        return date(year, int(s[2:4]), int(s[4:6]))
    raise ValueError(f"Cannot parse date from filename: {filename}")


def matches_patterns(filename: str) -> bool:
    return any(p.match(filename) for p in FILENAME_PATTERNS)


# ── URL fetching & download ─────────────────────────────────────────────


def fetch_xlsx_links(urls: list[str]) -> list[str]:
    """Fetch directory listings and extract xlsx hrefs matching patterns."""
    import re as _re
    all_links = set()
    for url in urls:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
        hrefs = _re.findall(r'href="(https?://[^"]+\.xlsx)"', resp.text, re.IGNORECASE)
        for h in hrefs:
            fname = h.rsplit("/", 1)[-1]
            if matches_patterns(fname):
                all_links.add(h)
    return sorted(all_links)


def download_file(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 1000:
        return True  # already downloaded
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=120, stream=True)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)
    return dest.exists() and dest.stat().st_size > 1000


# ── Row filtering ───────────────────────────────────────────────────────


def is_work_item(unique_id_val) -> bool:
    """Only rows with nonzero unique_id are work items."""
    if is_sentinel(unique_id_val):
        return False
    try:
        return int(float(unique_id_val)) != 0
    except (ValueError, TypeError):
        return False


# ── Ingest ──────────────────────────────────────────────────────────────


def file_already_processed(conn: sqlite3.Connection, source_file: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM work_plan_files WHERE source_file = ?", (source_file,)
    ).fetchone()
    return row is not None


def record_file_processed(
    conn: sqlite3.Connection, source_file: str, file_date: str,
    row_count: int, header_json: str,
):
    conn.execute(
        """INSERT INTO work_plan_files (source_file, file_date, row_count, header_json)
           VALUES (?, ?, ?, ?)""",
        (source_file, file_date, row_count, header_json),
    )


# The 21 columns we compare for duplicate detection
# (all columns except id, source_file, file_date, item_id, ingested_at)
COMPARE_COLS = [
    'unique_id', 'name', 'acronym', 'outline_level', 'release',
    'resource_names', 'start_date', 'finish_date', 'completion',
    'hyperlink', 'status_report', 'rapporteur_name', 'rapporteur_email',
    'notes', 'impacted_specs', 'tsg_approved', 'pcg_approved',
    'tsg_stopped', 'pcg_stopped', 'created', 'last_modified',
]


import hashlib


def compute_content_hash(vals: dict) -> str:
    """SHA-256 hash of the 21 compare column values."""
    parts = []
    for c in COMPARE_COLS:
        v = vals[c]
        parts.append(str(v) if v is not None else "")
    joined = "\x1f".join(parts)  # unit separator as delimiter
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def record_exists(conn: sqlite3.Connection, content_hash: str) -> bool:
    """Check if a record with identical content_hash exists."""
    row = conn.execute(
        "SELECT 1 FROM work_plan_items WHERE content_hash = ? LIMIT 1",
        (content_hash,),
    ).fetchone()
    return row is not None


def normalize_work_item(raw: dict) -> dict:
    """Normalize all 21 compare columns + item_id from raw values."""
    return {
        'item_id':         normalize_text(raw.get('id')),
        'unique_id':       normalize_text(raw.get('unique_id')),
        'name':            normalize_text(raw.get('name')) or '(empty)',
        'acronym':         normalize_text(raw.get('acronym')),
        'outline_level':   normalize_int(raw.get('outline_level')),
        'release':         normalize_text(raw.get('release')),
        'resource_names':  normalize_text(raw.get('resource_names')),
        'start_date':      normalize_timestamp(raw.get('start_date')),
        'finish_date':     normalize_timestamp(raw.get('finish_date')),
        'completion':      normalize_int(raw.get('completion')),
        'hyperlink':       normalize_text(raw.get('hyperlink')),
        'status_report':   normalize_text(raw.get('status_report')),
        'rapporteur_name': normalize_text(raw.get('rapporteur_name')),
        'rapporteur_email': normalize_text(raw.get('rapporteur_email')),
        'notes':           normalize_text(raw.get('notes')),
        'impacted_specs':  normalize_text(raw.get('impacted_specs')),
        'tsg_approved':    normalize_date(raw.get('tsg_approved')),
        'pcg_approved':    normalize_date(raw.get('pcg_approved')),
        'tsg_stopped':     normalize_date(raw.get('tsg_stopped')),
        'pcg_stopped':     normalize_date(raw.get('pcg_stopped')),
        'created':         normalize_timestamp(raw.get('created')),
        'last_modified':   normalize_timestamp(raw.get('last_modified')),
    }


def insert_work_item(
    conn: sqlite3.Connection, source_file: str, file_date: str,
    col_map: dict[int, str], raw_row: list,
):
    """Extract, normalize, and insert a single work item row.
    Skips if an identical record (by 21 compare columns) already exists."""
    # Build dict of {canonical_name: raw_value}
    raw = {}
    for col_idx, canon_name in col_map.items():
        raw[canon_name] = raw_row[col_idx] if col_idx < len(raw_row) else None

    vals = normalize_work_item(raw)
    chash = compute_content_hash(vals)

    # Skip if identical record already exists
    if record_exists(conn, chash):
        return None

    conn.execute(
        """INSERT INTO work_plan_items (
            source_file, file_date, item_id, unique_id, name, acronym,
            outline_level, release, resource_names, start_date, finish_date,
            completion, hyperlink, status_report, rapporteur_name,
            rapporteur_email, notes, impacted_specs, tsg_approved,
            pcg_approved, tsg_stopped, pcg_stopped, created, last_modified,
            content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source_file, file_date,
            vals['item_id'], vals['unique_id'], vals['name'],
            vals['acronym'], vals['outline_level'], vals['release'],
            vals['resource_names'], vals['start_date'], vals['finish_date'],
            vals['completion'], vals['hyperlink'], vals['status_report'],
            vals['rapporteur_name'], vals['rapporteur_email'],
            vals['notes'], vals['impacted_specs'],
            vals['tsg_approved'], vals['pcg_approved'],
            vals['tsg_stopped'], vals['pcg_stopped'],
            vals['created'], vals['last_modified'],
            chash,
        ),
    )
    return vals


def process_file(conn: sqlite3.Connection, fpath: Path) -> tuple[int, list[dict]]:
    """Process a single xlsx file. Returns (count, list_of_new_items)."""
    source_file = fpath.name
    file_date = parse_file_date(source_file).isoformat()

    if file_already_processed(conn, source_file):
        print(f"  SKIP (already processed): {source_file}")
        return -1, []

    print(f"  Processing: {source_file} ({file_date})")

    wb = load_workbook(fpath, read_only=True, data_only=True)
    inserted = 0
    new_items: list[dict] = []
    first_header_json = None

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]

        # Skip empty sheets
        try:
            first_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        except StopIteration:
            first_row = None
        if first_row is None:
            continue

        headers = [str(h) if h is not None else "" for h in first_row]

        # Skip sheets without a proper header (no "ID" in first column)
        if not headers or headers[0].strip().lower() != "id":
            continue

        col_map = build_column_map(headers)

        # Verify all 22 columns matched
        if len(col_map) < 22:
            unmatched = [headers[i] for i in range(len(headers)) if i not in col_map]
            print(f"    WARNING: {len(col_map)}/22 columns matched in sheet '{sheet_name}'. Unmatched: {unmatched}")

        if first_header_json is None:
            import json
            first_header_json = json.dumps(headers)

        sheet_inserted = 0
        for raw_row in ws.iter_rows(min_row=2, values_only=True):
            raw_list = list(raw_row)
            uid_raw = raw_list[1] if len(raw_list) > 1 else None
            if not is_work_item(uid_raw):
                continue
            result = insert_work_item(conn, source_file, file_date, col_map, raw_list)
            if result is not None:
                sheet_inserted += 1
                new_items.append(result)

        if sheet_inserted > 0 and sheet_name != wb.sheetnames[0]:
            print(f"    Sheet '{sheet_name}': {sheet_inserted} work items")

        inserted += sheet_inserted

    wb.close()

    # Record file as processed
    record_file_processed(conn, source_file, file_date, inserted, first_header_json or "[]")
    conn.commit()

    print(f"    Inserted {inserted} work items")
    return inserted, new_items


# ── Email report ───────────────────────────────────────────────────────

# Columns shown in the email report, in display order
REPORT_COLS = [
    ("item_id",         "Item ID"),
    ("unique_id",       "Unique ID"),
    ("name",            "Name"),
    ("acronym",         "Acronym"),
    ("release",         "Release"),
    ("start_date",      "Start Date"),
    ("finish_date",     "Finish Date"),
    ("completion",      "Completion %"),
    ("rapporteur_name", "Rapporteur"),
    ("rapporteur_email","Rapporteur Email"),
    ("status_report",   "Status Report"),
    ("hyperlink",       "Hyperlink"),
    ("resource_names",  "Resources"),
    ("notes",           "Notes"),
]


def build_email_body(new_items: list[dict]) -> str:
    """Build a plain-text email body listing the newly added work plan items."""
    lines = [
        f"3GPP Work Plan - {len(new_items)} new work plan item(s) were added.",
        "",
        f"Generated: {datetime.now().isoformat()}",
        "",
    ]
    for i, item in enumerate(new_items, 1):
        lines.append(f"{'=' * 70}")
        lines.append(f"Item {i}:")
        for key, label in REPORT_COLS:
            val = item.get(key)
            if val is not None and str(val).strip():
                lines.append(f"  {label}: {val}")
        lines.append("")
    lines.append(f"{'=' * 70}")
    lines.append("")
    lines.append(f"Total new items: {len(new_items)}")
    return "\n".join(lines)


def send_report_email(new_items: list[dict]):
    """Send an email report about newly added work plan items."""
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = os.environ.get("SMTP_PORT", "")
    smtp_ssltls = os.environ.get("SMTP_SSLTLS", "").strip().lower() in (
        "1", "true", "yes", "ssl", "tls", "ssl/tls", "starttls",
    )
    smtp_user = os.environ.get("SMTP_USERNAME", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    to_raw = os.environ.get("TO", "")

    if not smtp_host or not smtp_port or not to_raw:
        print("Email report skipped: SMTP settings or TO not configured in .env")
        return

    recipients = [addr.strip() for addr in to_raw.split(",") if addr.strip()]
    if not recipients:
        print("Email report skipped: no recipients in TO")
        return

    subject = f"[3GPP Work Plan] {len(new_items)} new work plan item(s) added"
    body = build_email_body(new_items)

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    context = ssl.create_default_context()
    try:
        if smtp_ssltls:
            if int(smtp_port) == 465:
                # Implicit TLS: the connection is encrypted from the start.
                with smtplib.SMTP_SSL(smtp_host, 465, context=context) as server:
                    if smtp_user and smtp_pass:
                        server.login(smtp_user, smtp_pass)
                    server.sendmail(smtp_user, recipients, msg.as_string())
            else:
                # Explicit TLS (STARTTLS): connect in plaintext, then upgrade,
                # as used on ports such as 587.
                with smtplib.SMTP(smtp_host, int(smtp_port)) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    if smtp_user and smtp_pass:
                        server.login(smtp_user, smtp_pass)
                    server.sendmail(smtp_user, recipients, msg.as_string())
        else:
            # No encryption requested; connect in plaintext.
            with smtplib.SMTP(smtp_host, int(smtp_port)) as server:
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, recipients, msg.as_string())
        print(f"Report email sent to {', '.join(recipients)}")
    except Exception as e:
        print(f"Failed to send report email: {e}")


# ── Main ────────────────────────────────────────────────────────────────


def main():
    XLSX_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize SQLite database
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_SQL)
    conn.commit()

    # Step 1: Fetch links from URLs
    print("Fetching xlsx links from 3GPP URLs...")
    links = fetch_xlsx_links(URLS)
    print(f"  Found {len(links)} matching xlsx files")

    # Step 2: Download new files
    print("\nChecking for new files to download...")
    new_downloads = 0
    file_paths = []
    for url in links:
        fname = url.rsplit("/", 1)[-1]
        dest = XLSX_DIR / fname
        if not (dest.exists() and dest.stat().st_size > 1000):
            print(f"  Downloading: {fname}")
            if download_file(url, dest):
                new_downloads += 1
            else:
                print(f"    FAILED to download: {fname}")
                continue
        file_paths.append(dest)

    print(f"  Downloaded {new_downloads} new files, {len(file_paths)} total")

    # Step 3: Sort chronologically
    file_paths.sort(key=lambda p: parse_file_date(p.name))

    # Step 4: Process each file
    print("\nProcessing files in chronological order...")
    total_inserted = 0
    all_new_items: list[dict] = []
    for fpath in file_paths:
        result, new_items = process_file(conn, fpath)
        if result >= 0:
            total_inserted += result
            all_new_items.extend(new_items)

    # Summary
    print(f"\nDone. Total work items inserted this run: {total_inserted}")

    # Step 5: Send email report if new items were added
    print(f"\nSending email report for {len(all_new_items)} new work plan item(s)...")
    send_report_email(all_new_items)

    # DB stats
    file_count = conn.execute("SELECT COUNT(*) FROM work_plan_files").fetchone()[0]
    item_count = conn.execute("SELECT COUNT(*) FROM work_plan_items").fetchone()[0]
    print(f"Database: {DB_PATH}")
    print(f"  Files processed: {file_count}")
    print(f"  Work items:      {item_count}")

    conn.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
3GPP Work Plan Web App.

A small Flask backend + single-page frontend over the snapshot history in
``threegpp.sqlite3`` (populated by ingest.py).  Read-only against the DB.

Features
--------
* View the latest work plan snapshot.
* Search / filter / sort / paginate work plan items within a snapshot.
* Pick an arbitrary date/time and view the work plan as of that moment
  (the latest snapshot whose file_date <= the chosen moment).
* Select a work plan item and view its full history across all snapshots
  (keyed on the stable ``unique_id``).

Run
---
    python3 app.py            # serves on http://127.0.0.1:5000
"""

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, abort

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "threegpp.sqlite3"
STATIC_DIR = BASE_DIR / "static"

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")


def db():
    """Open a read-only SQLite connection, one per request."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def fmt_date(value):
    """Pretty YYYY-MM-DD display, tolerating non-date junk (e.g. 'TSG#71')."""
    if not value:
        return ""
    text = str(value).strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    return match.group(0) if match else text


def list_snapshots():
    """Return [{file_date, source_file, row_count}] sorted ascending."""
    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT file_date, source_file, COUNT(*) AS row_count
            FROM work_plan_items
            GROUP BY file_date, source_file
            ORDER BY file_date
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def resolve_snapshot(datetime_str=None):
    """Return the snapshot applying at the given moment.

    Defaults to the latest snapshot.  When ``datetime_str`` is given, returns
    the latest snapshot whose file_date is <= that moment (the "as of" view),
    clamping to the earliest snapshot if the moment precedes history.
    """
    snapshots = list_snapshots()
    if not snapshots:
        return None

    clamped_early = False
    if not datetime_str:
        chosen = snapshots[-1]
        requested = None
    else:
        raw = str(datetime_str).strip()
        moment = None
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                moment = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if moment is None:
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
            if m:
                moment = datetime.strptime(m.group(0), "%Y-%m-%d")
        if moment is None:
            return None

        chosen = None
        for snap in snapshots:
            d = datetime.strptime(snap["file_date"], "%Y-%m-%d")
            if d <= moment:
                chosen = snap
            else:
                break
        if chosen is None:  # moment precedes all history -> clamp to earliest
            chosen = snapshots[0]
            clamped_early = True
        requested = moment.isoformat()

    out = dict(chosen)
    if requested is not None:
        out["requested"] = requested
    out["clamped_early"] = clamped_early
    # Number of distinct work items whose latest version exists at this moment.
    out["item_count"] = asof_item_count(out["file_date"])
    return out


def _asof_subquery():
    """SQL subquery returning the latest version of every unique work item
    as of a given moment.  Single '?' parameter = cutoff file_date.

    This is a point-in-time reconstruction: each unique_id contributes exactly
    one row — the most recent snapshot (<= cutoff) for that item.
    """
    return """
        SELECT * FROM (
            SELECT wpi.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY unique_id
                       ORDER BY file_date DESC, id DESC
                   ) AS rn
            FROM work_plan_items wpi
            WHERE wpi.file_date <= ?
        ) ASof
        WHERE ASof.rn = 1
    """


def asof_item_count(file_date):
    """Count distinct work items whose latest version exists at/equal file_date."""
    conn = db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT unique_id FROM work_plan_items "
            "  WHERE file_date <= ? GROUP BY unique_id) ;",
            (file_date,),
        ).fetchone()[0]
    finally:
        conn.close()


# Whitelist for ORDER BY.  The *keys* are the only sort identifiers accepted
# from the client; the *values* are hard-coded SQL fragments that are never
# derived from user input.  This is the single source of truth for sorting and
# is the key defense against SQL injection via the `sort` / `order` params.
SORTABLE = {
    "id": "t.id",
    "name": "t.name COLLATE NOCASE",
    "acronym": "t.acronym COLLATE NOCASE",
    "item_id": "CAST(t.item_id AS INTEGER)",
    "unique_id": "t.unique_id",
    "release": "t.release COLLATE NOCASE",
    "outline_level": "t.outline_level",
    "completion": "t.completion",
    "start_date": "t.start_date",
    "finish_date": "t.finish_date",
}
DEFAULT_SORT = "id"
ORDER_DIRECTIONS = ("asc", "desc")


def _like(value):
    """Escape LIKE wildcards so user input is matched literally.

    A leading/trailing backslash would otherwise be interpreted as an escape
    character by the `ESCAPE '\\'` clause, so we double every backslash first.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _safe_int(value):
    """Coerce a request value to int, returning None for anything malformed.

    Prevents invalid client input (e.g. ``?outline_level=abc``) from becoming a
    raw ``int()`` / ``float()`` exception (HTTP 500) and guarantees that only a
    plain integer ever reaches the parameterised SQL binder.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value):
    """Coerce a request value to float, returning None for anything malformed.

    Rejects non-finite values such as ``nan``/``inf`` which could otherwise be
    smuggled in as a number and used to perturb an indexed comparison.
    """
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def query_items(snapshot_date, params):
    """Search/filter/sort/paginate over the point-in-time (as-of) work plan.

    ``snapshot_date`` is the cutoff moment.  All filters and the count apply to
    the reconstructed set of every unique work item (latest version as of the
    cutoff), not just the rows present in a single snapshot file.
    """
    asof = _asof_subquery()  # has one '?' = snapshot_date
    where = []
    args = [snapshot_date]

    q = (params.get("q") or "").strip()
    if q:
        like = "%" + _like(q.lower()) + "%"
        where.append(
            """(
                LOWER(COALESCE(t.name,'')) LIKE ? ESCAPE '\\'
                OR LOWER(COALESCE(t.acronym,'')) LIKE ? ESCAPE '\\'
                OR COALESCE(t.item_id,'') LIKE ? ESCAPE '\\'
                OR COALESCE(t.unique_id,'') LIKE ? ESCAPE '\\'
                OR LOWER(COALESCE(t.status_report,'')) LIKE ? ESCAPE '\\'
                OR LOWER(COALESCE(t.notes,'')) LIKE ? ESCAPE '\\'
                OR LOWER(COALESCE(t.rapporteur_name,'')) LIKE ? ESCAPE '\\'
                OR LOWER(COALESCE(t.impacted_specs,'')) LIKE ? ESCAPE '\\'
            )"""
        )
        args += [like] * 8

    # Release filter supports multiple comma-separated values (OR / IN).
    release = (params.get("release") or "").strip()
    if release and release.lower() != "all":
        rels = [r.strip() for r in release.split(",") if r.strip()]
        if rels:
            placeholders = ",".join(["?"] * len(rels))
            where.append(f"t.release IN ({placeholders})")
            args.extend(rels)

    outline = (params.get("outline_level") or "").strip()
    outline_int = _safe_int(outline)
    if outline_int is not None and outline.lower() != "all":
        where.append("t.outline_level = ?")
        args.append(int(outline_int))

    rapporteur = (params.get("rapporteur") or "").strip()
    if rapporteur and rapporteur.lower() != "all":
        where.append("LOWER(COALESCE(t.rapporteur_name,'')) = LOWER(?)")
        args.append(rapporteur)

    acronym = (params.get("acronym") or "").strip()
    if acronym and acronym.lower() != "all":
        where.append("LOWER(COALESCE(t.acronym,'')) = LOWER(?)")
        args.append(acronym)

    comp_min = params.get("completion_min")
    comp_max = params.get("completion_max")
    comp_min_f = _safe_float(comp_min)
    comp_max_f = _safe_float(comp_max)
    if comp_min_f is not None:
        where.append("t.completion >= ?")
        args.append(comp_min_f)
    if comp_max_f is not None:
        where.append("t.completion <= ?")
        args.append(comp_max_f)

    # Multiple sort criteria: comma-separated columns + comma-separated
    # directions, matched by position (priority order: first = primary).
    #
    # SECURITY: ``col`` is only ever used as a lookup key into SORTABLE and is
    # NEVER interpolated into the SQL string directly.  The expression placed in
    # the ORDER BY comes exclusively from the whitelist values, and the
    # direction is coerced to a literal ASC/DESC.  Anything not recognised is
    # dropped.  This closes the classic ORDER BY / paren injection vector.
    sort_columns = (params.get("sort") or DEFAULT_SORT).split(",")
    order_values = (params.get("order") or "asc").split(",")
    applied = []
    order_by = []
    for i, col in enumerate(sort_columns):
        col = col.strip()
        if col not in SORTABLE:
            continue
        raw_order = "asc"
        if i < len(order_values):
            raw_order = order_values[i].strip().lower()
        else:
            # repeat the last supplied direction if fewer orders than columns
            for v in reversed(order_values):
                if v.strip():
                    raw_order = v.strip().lower()
                    break
        if raw_order not in ORDER_DIRECTIONS:
            raw_order = "asc"
        applied.append({"col": col, "order": raw_order})
        order_by.append(f"{SORTABLE[col]} {raw_order.upper()}")
    if not applied:  # fallback default sort
        applied.append({"col": DEFAULT_SORT, "order": "asc"})
        order_by.append(f"{SORTABLE[DEFAULT_SORT]} ASC")
    order_by_sql = ", ".join(order_by)

    try:
        limit = min(int(params.get("limit", 25)), 100)
    except (TypeError, ValueError):
        limit = 25
    try:
        offset = max(int(params.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0

    where_sql = (" AND ".join(where)) if where else "1=1"

    conn = db()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM ({asof}) AS t WHERE {where_sql}", args
        ).fetchone()[0]

        sql = f"""
            SELECT * FROM ({asof}) AS t
            WHERE {where_sql}
            ORDER BY {order_by_sql}
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(sql, args + [limit, offset]).fetchall()
        items = [_row_to_dict(r) for r in rows]
        return {"total": total, "limit": limit, "offset": offset,
                "items": items, "sorts": applied}
    finally:
        conn.close()


def _row_to_dict(row):
    d = dict(row)
    for key in ("start_date", "finish_date", "created", "last_modified",
                "tsg_approved", "pcg_approved", "tsg_stopped", "pcg_stopped",
                "file_date"):
        d[key] = fmt_date(d.get(key))
    return d


def get_facets(snapshot_date):
    """Distinct release values over the as-of work plan at snapshot_date,
    used to populate the release multi-select."""
    asof = _asof_subquery()
    conn = db()
    try:
        releases = [r["v"] for r in conn.execute(
            f"SELECT DISTINCT t.release AS v FROM ({asof}) AS t "
            "WHERE t.release IS NOT NULL AND t.release != '' "
            "ORDER BY t.release", (snapshot_date,)).fetchall()]
        return {"releases": releases}
    finally:
        conn.close()



def get_item_history(unique_id):
    """Full details come from the latest snapshot; history spans all snapshots."""
    conn = db()
    try:
        history = conn.execute(
            """
            SELECT file_date, source_file, name, acronym, release,
                   outline_level, completion, start_date, finish_date,
                   tsg_approved, status_report, hyperlink
            FROM work_plan_items
            WHERE unique_id = ?
            ORDER BY file_date
            """,
            (str(unique_id),),
        ).fetchall()
        if not history:
            return None

        latest = history[-1]
        detail = conn.execute(
            "SELECT * FROM work_plan_items "
            "WHERE unique_id = ? ORDER BY file_date DESC LIMIT 1",
            (str(unique_id),),
        ).fetchone()

        out = {
            "unique_id": str(unique_id),
            "name": latest["name"],
            "acronym": latest["acronym"],
            "latest": _row_to_dict(dict(detail)) if detail else {},
            "history": [
                {
                    "file_date": r["file_date"],
                    "source_file": r["source_file"],
                    "name": r["name"],
                    "acronym": r["acronym"],
                    "release": r["release"],
                    "outline_level": r["outline_level"],
                    "completion": r["completion"],
                    "start_date": fmt_date(r["start_date"]),
                    "finish_date": fmt_date(r["finish_date"]),
                    "tsg_approved": fmt_date(r["tsg_approved"]),
                    "status_report": r["status_report"],
                    "hyperlink": r["hyperlink"],
                }
                for r in history
            ],
        }
        return out
    finally:
        conn.close()


@app.after_request
def add_security_headers(resp):
    """Set security-related HTTP headers on every response."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'",
    )
    # API responses carry snapshot data; never let intermediate caches store it.
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/snapshots")
def api_snapshots():
    return jsonify({"snapshots": list_snapshots()})


@app.route("/api/resolve-snapshot")
def api_resolve_snapshot():
    snap = resolve_snapshot(request.args.get("datetime"))
    if snap is None:
        return jsonify({"error": "no snapshots available"}), 404
    return jsonify(snap)


@app.route("/api/facets")
def api_facets():
    snap = resolve_snapshot(request.args.get("snapshot"))
    if snap is None:
        return jsonify({"error": "no snapshots available"}), 404
    return jsonify(get_facets(snap["file_date"]))


@app.route("/api/items")
def api_items():
    snap = resolve_snapshot(request.args.get("snapshot"))
    if snap is None:
        return jsonify({"error": "no snapshots available"}), 404
    result = query_items(snap["file_date"], request.args)
    return jsonify({"snapshot": snap, **result})


@app.route("/api/items/<path:unique_id>")
def api_item_history(unique_id):
    data = get_item_history(unique_id)
    if data is None:
        abort(404, description=f"No work plan item with unique_id {unique_id}")
    return jsonify(data)


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        # Return a generic message rather than echoing back the raw
        # path / unique_id supplied by the caller.
        return jsonify({"error": "not found"}), 404
    return send_from_directory(STATIC_DIR, "index.html")


if __name__ == "__main__":
    # Bind to loopback by default so this unauthenticated, read-only data
    # service is not accidentally exposed to the whole network.  Set HOST
    # (e.g. HOST=0.0.0.0) explicitly only if remote access is actually wanted.
    host = os.environ.get("HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("PORT", 5000))
    except (TypeError, ValueError):
        port = 5000
    print(f"3GPP Work Plan web app -> http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


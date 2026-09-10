#!/usr/bin/env python3
"""3GPP Work Plan viewer.

Shows the latest known state of every work item. The database holds one row
per (snapshot file, item); the same item (keyed by ``unique_id``) appears in
many snapshots. The default view collapses those versions to a single row:
the one from the newest snapshot (ties broken by highest row id).

Run:  python3 app.py   ->  http://127.0.0.1:5917
"""

import os
import re
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, request

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "threegpp.sqlite3"

app = Flask(__name__)

# One row per item: newest file_date wins, highest id breaks ties
# (two files can share a file_date; a few old files repeat a unique_id
# for parent + sub-task rows, where highest id = deepest sub-task row).
LATEST = """
    SELECT * FROM (
        SELECT w.*,
               ROW_NUMBER() OVER (
                   PARTITION BY w.unique_id
                   ORDER BY w.file_date DESC, w.id DESC
               ) AS _rn
        FROM work_plan_items w
    )
    WHERE _rn = 1
"""

def release_ord(value):
    """R99 first, then Rel-N sorted by N as an integer."""
    if value is None:
        return (2, 0, "")
    v = str(value).strip()
    if v == "R99":
        return (0, 0, v)
    m = re.fullmatch(r"Rel-(\d+)", v, re.IGNORECASE)
    if m:
        return (1, int(m.group(1)), v)
    return (2, 0, v)


SORTABLE = {
    "unique_id": "t.unique_id",
    "name": "t.name COLLATE NOCASE",
    "acronym": "t.acronym COLLATE NOCASE",
    # (subdir, subkey): subdir carries ASC/DESC so desc fully reverses.
    "release": ("(CASE WHEN t.release = 'R99' THEN 0"
                  " WHEN LOWER(t.release) GLOB 'rel-[0-9]*'"
                  " THEN 1000000 + CAST(SUBSTR(t.release, 5) AS INTEGER)"
                  " ELSE 2000000 END, t.release COLLATE NOCASE)", True),
    "outline_level": "t.outline_level",
    "completion": "t.completion",
    "start_date": "t.start_date",
    "finish_date": "t.finish_date",
    "file_date": "t.file_date",
}

SEARCH_COLS = (
    "t.name", "t.acronym", "t.unique_id", "t.item_id",
    "t.status_report", "t.notes", "t.rapporteur_name",
)

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def db():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def short_date(value):
    if value is None:
        return ""
    m = DATE_RE.search(str(value))
    return m.group(0) if m else str(value)


def fmt_pct(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    try:
        return f"{int(value)}%"
    except (ValueError, TypeError):
        return str(value)


def row_dict(row):
    d = dict(row)
    d.pop("_rn", None)
    for k in ("start_date", "finish_date", "created", "last_modified",
              "tsg_approved", "pcg_approved", "tsg_stopped", "pcg_stopped",
              "file_date"):
        if k in d:
            d[k] = short_date(d.get(k))
    if "completion" in d:
        d["completion"] = fmt_pct(d.get("completion"))
    return d


def like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clean_date(value):
    v = (value or "").strip()
    return v if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else None


def to_int(value, default, lo, hi):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'",
    )
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.route("/")
def index():
    return PAGE


@app.route("/api/meta")
def api_meta():
    conn = db()
    try:
        r = conn.execute(
            "SELECT COUNT(*) AS rows, COUNT(DISTINCT unique_id) AS items,"
            " COUNT(DISTINCT source_file) AS files,"
            " COUNT(DISTINCT file_date) AS dates,"
            " MIN(file_date) AS earliest, MAX(file_date) AS latest"
            " FROM work_plan_items"
        ).fetchone()
        dates = [d["v"] for d in conn.execute(
            "SELECT DISTINCT file_date AS v FROM work_plan_items ORDER BY v"
        ).fetchall()]
        out = {k: r[k] for k in r.keys()}
        out["dates"] = dates
        return jsonify(out)
    finally:
        conn.close()


@app.route("/api/releases")
def api_releases():
    conn = db()
    try:
        rows = conn.execute(
            f"SELECT DISTINCT t.release AS v FROM ({LATEST}) AS t"
            " WHERE t.release IS NOT NULL AND t.release != ''"
        ).fetchall()
        return jsonify({"releases": sorted(
            (r["v"] for r in rows), key=release_ord)})
    finally:
        conn.close()


@app.route("/api/items")
def api_items():
    where, args = [], []
    q = (request.args.get("q") or "").strip()
    if q:
        like = "%" + like_escape(q.lower()) + "%"
        ors = " OR ".join(
            f"LOWER(COALESCE({c},'')) LIKE ? ESCAPE '\\'" for c in SEARCH_COLS
        )
        where.append(f"({ors})")
        args += [like] * len(SEARCH_COLS)

    releases = [r.strip() for r in (request.args.get("release") or "").split(",")]
    releases = [r for r in releases if r and r.lower() != "all"]
    if releases:
        where.append("t.release IN (%s)" % ",".join(["?"] * len(releases)))
        args.extend(releases)

    ver_from = clean_date(request.args.get("ver_from"))
    ver_to = clean_date(request.args.get("ver_to"))
    if ver_from:
        where.append("t.file_date >= ?")
        args.append(ver_from)
    if ver_to:
        where.append("t.file_date <= ?")
        args.append(ver_to)

    sort = (request.args.get("sort") or "unique_id").strip()
    if sort not in SORTABLE:
        sort = "unique_id"
    order = (request.args.get("order") or "asc").strip().lower()
    if order not in ("asc", "desc"):
        order = "asc"

    limit = to_int(request.args.get("limit"), 50, 1, 200)
    offset = to_int(request.args.get("offset"), 0, 0, 10**9)
    cond = " AND ".join(where) if where else "1=1"

    conn = db()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM ({LATEST}) AS t WHERE {cond}", args
        ).fetchone()[0]
        order_entry = SORTABLE[sort]
        if isinstance(order_entry, tuple):
            order_expr, sub = order_entry
            parts = [p.strip() for p in order_expr.strip("()").rsplit(",", 1)]
            order_sql = ", ".join(f"{p} {order.upper()}" for p in parts)
        else:
            order_sql = f"{order_entry} {order.upper()}"
        rows = conn.execute(
            f"SELECT * FROM ({LATEST}) AS t WHERE {cond}"
            f" ORDER BY {order_sql}"
            " LIMIT ? OFFSET ?",
            args + [limit, offset],
        ).fetchall()
        return jsonify({
            "total": total, "limit": limit, "offset": offset,
            "sort": sort, "order": order,
            "items": [row_dict(r) for r in rows],
        })
    finally:
        conn.close()


@app.route("/api/history/<unique_id>")
def api_history(unique_id):
    uid = str(unique_id).strip()
    if not uid:
        return jsonify({"error": "empty unique_id"}), 400
    conn = db()
    try:
        hist = conn.execute(
            "SELECT file_date, source_file, item_id, name, acronym, release,"
            " outline_level, completion, start_date, finish_date,"
            " status_report, hyperlink, rapporteur_name"
            " FROM work_plan_items WHERE unique_id = ?"
            " ORDER BY file_date, id",
            (uid,),
        ).fetchall()
        if not hist:
            return jsonify({"error": "not found"}), 404
        latest = conn.execute(
            "SELECT * FROM work_plan_items WHERE unique_id = ?"
            " ORDER BY file_date DESC, id DESC LIMIT 1",
            (uid,),
        ).fetchone()
        out = [dict(h) for h in hist]
        for h in out:
            for k in ("start_date", "finish_date", "file_date"):
                h[k] = short_date(h.get(k))
            h["completion"] = fmt_pct(h.get("completion"))
        d = dict(latest)
        d.pop("content_hash", None)
        return jsonify({
            "unique_id": uid,
            "name": out[-1]["name"],
            "acronym": out[-1]["acronym"],
            "latest": row_dict(latest),
            "history": out,
        })
    finally:
        conn.close()


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>3GPP Work Plan</title>
<style>
:root { --bg:#0f1520; --panel:#18202f; --panel2:#1f2939; --line:#2b3748;
  --tx:#e7edf5; --mut:#93a2b4; --ac:#3f9bff; --ac2:#7cc4ff; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--tx);
  font:14px/1.45 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
header { padding:18px 24px; border-bottom:1px solid var(--line); }
header h1 { margin:0; font-size:20px; }
header p { margin:4px 0 0; color:var(--mut); font-size:13px; }
.card { background:var(--panel); border:1px solid var(--line);
  border-radius:12px; margin:14px 24px; padding:16px; }
.row { display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; }
.field { display:flex; flex-direction:column; gap:4px; }
.field.grow { flex:1 1 240px; }
label { font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:var(--mut); }
input,select { background:var(--panel2); border:1px solid var(--line);
  color:var(--tx); border-radius:8px; padding:8px 10px; }
input:focus,select:focus { outline:none; border-color:var(--ac); }
button { background:var(--ac); color:#06121f; border:none; border-radius:8px;
  padding:8px 14px; font-weight:600; cursor:pointer; }
button.ghost { background:transparent; color:var(--tx); border:1px solid var(--line); }
button:disabled { opacity:.4; cursor:default; }
.info { margin-left:auto; color:var(--mut); font-size:13px; }
.info b { color:var(--tx); }
.wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; }
th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); }
th { color:var(--mut); font-size:11px; text-transform:uppercase; letter-spacing:.5px;
  white-space:nowrap; }
tbody tr { cursor:pointer; }
tbody tr:hover { background:var(--panel2); }
td.nm { font-weight:600; }
td.ac { color:var(--ac2); font-family:ui-monospace,monospace; font-size:13px; }
td.ver { color:var(--mut); white-space:nowrap; }
.empty { text-align:center; color:var(--mut); padding:36px; }
.pager { display:flex; gap:8px; align-items:center; justify-content:center;
  margin-top:12px; color:var(--mut); }
#detail { position:fixed; top:0; right:0; height:100%; width:min(880px,96vw);
  background:var(--panel); border-left:1px solid var(--line); z-index:50;
  transform:translateX(102%); transition:transform .22s ease;
  display:flex; flex-direction:column; }
#detail.open { transform:translateX(0); }
#detail .hd { display:flex; justify-content:space-between; gap:10px;
  padding:16px 20px; border-bottom:1px solid var(--line); }
#detail .hd h2 { margin:0; font-size:17px; }
#detail .bd { padding:14px 20px; overflow-y:auto; }
#overlay { position:fixed; inset:0; background:rgba(5,9,15,.6); z-index:40; }
.hidden { display:none !important; }
.kv { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
  gap:8px; margin:12px 0; }
.kv div { background:var(--panel2); border:1px solid var(--line);
  border-radius:8px; padding:7px 10px; }
.kv small { display:block; color:var(--mut); font-size:10px;
  text-transform:uppercase; letter-spacing:.5px; }
.mut { color:var(--mut); }
details { position:relative; }
details summary { background:var(--panel2); border:1px solid var(--line);
  color:var(--tx); border-radius:8px; padding:8px 10px; cursor:pointer;
  min-width:150px; list-style:none; }
details summary::-webkit-details-marker { display:none; }
#relList { position:absolute; top:calc(100% + 4px); left:0; z-index:70;
  background:var(--panel2); border:1px solid var(--line); border-radius:8px;
  padding:6px; max-height:260px; overflow-y:auto; min-width:100%; }
#relList label { display:flex; gap:8px; align-items:center; padding:4px 8px;
  border-radius:6px; cursor:pointer; font-size:13px; text-transform:none;
  letter-spacing:0; color:var(--tx); white-space:nowrap; }
#relList label:hover { background:var(--panel); }
#verFrom,#verTo { max-width:130px; }
#toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%);
  background:#0c121c; border:1px solid var(--line); border-radius:8px;
  padding:10px 16px; z-index:90; }
</style>
</head>
<body>
<header>
  <h1>3GPP Work Plan</h1>
  <p id="meta">Latest known state of every work item.</p>
</header>

<section class="card">
  <div class="row">
    <div class="field grow">
      <label for="q">Search</label>
      <input id="q" type="search" placeholder="name, acronym, ID, note, rapporteur" />
    </div>
    <div class="field">
      <label>Release</label>
      <details id="relDrop">
        <summary id="relLabel">All releases</summary>
        <div id="relList"></div>
      </details>
    </div>
    <div class="field">
      <label for="verFrom">Version from</label>
      <select id="verFrom"><option value="">Any</option></select>
    </div>
    <div class="field">
      <label for="verTo">Version to</label>
      <select id="verTo"><option value="">Any</option></select>
    </div>
    <div class="field">
      <label for="sort">Sort by</label>
      <select id="sort">
        <option value="unique_id">Unique ID</option>
        <option value="name">Name</option>
        <option value="acronym">Acronym</option>
        <option value="release">Release</option>
        <option value="outline_level">Level</option>
        <option value="completion">Completion</option>
        <option value="start_date">Start</option>
        <option value="finish_date">Finish</option>
        <option value="file_date">Version date</option>
      </select>
    </div>
    <div class="field">
      <label for="ord">Direction</label>
      <select id="ord"><option value="asc">Asc</option><option value="desc">Desc</option></select>
    </div>
    <div class="field">
      <label for="per">Per page</label>
      <select id="per">
        <option value="25">25</option>
        <option value="50" selected>50</option>
        <option value="100">100</option>
        <option value="200">200</option>
      </select>
    </div>
    <div class="field"><label>&nbsp;</label><button id="reset" class="ghost">Reset</button></div>
    <div class="info" id="info"></div>
  </div>
</section>

<section class="card">
  <div class="wrap">
    <table>
      <thead><tr>
        <th>Unique ID</th><th>Name</th><th>Acronym</th><th>Release</th>
        <th>Level</th><th>Compl</th><th>Start</th><th>Finish</th>
        <th>Rapporteur</th><th>Version</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <div class="pager">
    <button id="prev">&lt; Prev</button>
    <span id="page"></span>
    <button id="next">Next &gt;</button>
  </div>
</section>

<div id="overlay" class="hidden"></div>
<aside id="detail">
  <div class="hd">
    <div><h2 id="dTitle"></h2><span id="dSub" class="mut"></span></div>
    <div><button id="dClose" class="ghost">Close</button></div>
  </div>
  <div class="bd">
    <div id="dBody"></div>
    <h3>Version history</h3>
    <p class="mut">Every snapshot this item appeared in, oldest first.</p>
    <div class="wrap"><table>
      <thead><tr><th>Snapshot</th><th>Name</th><th>Release</th><th>Level</th>
      <th>Compl</th><th>Start</th><th>Finish</th><th>Status</th></tr></thead>
      <tbody id="dHist"></tbody>
    </table></div>
  </div>
</aside>

<div id="toast" class="hidden"></div>

<script>
"use strict";
const $ = (s) => document.querySelector(s);
const els = { q:$("#q"), relDrop:$("#relDrop"), relLabel:$("#relLabel"),
  relList:$("#relList"), verFrom:$("#verFrom"), verTo:$("#verTo"),
  sort:$("#sort"), ord:$("#ord"), per:$("#per"),
  info:$("#info"), rows:$("#rows"), page:$("#page"), prev:$("#prev"),
  next:$("#next"), meta:$("#meta"), reset:$("#reset"),
  overlay:$("#overlay"), detail:$("#detail"), dTitle:$("#dTitle"),
  dSub:$("#dSub"), dBody:$("#dBody"), dHist:$("#dHist"),
  dClose:$("#dClose"), toast:$("#toast") };
const st = { q:"", releases:[], verFrom:"", verTo:"",
  sort:"unique_id", order:"asc", offset:0, total:0, per:50 };

const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function say(m) { els.toast.textContent = m; els.toast.classList.remove("hidden");
  clearTimeout(say.t); say.t = setTimeout(() => els.toast.classList.add("hidden"), 2500); }
async function get(url) { const r = await fetch(url);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
  return r.json(); }

function query() {
  const p = new URLSearchParams();
  if (st.q) p.set("q", st.q);
  if (st.releases.length) p.set("release", st.releases.join(","));
  if (st.verFrom) p.set("ver_from", st.verFrom);
  if (st.verTo) p.set("ver_to", st.verTo);
  p.set("sort", st.sort); p.set("order", st.order);
  p.set("limit", st.per); p.set("offset", st.offset);
  return p.toString();
}

async function load() {
  const d = await get("/api/items?" + query());
  st.total = d.total;
  els.rows.innerHTML = d.items.length ? d.items.map((t) =>
    `<tr data-uid="${esc(t.unique_id)}"><td>${esc(t.unique_id)}</td>` +
    `<td class="nm">${esc(t.name)}</td><td class="ac">${esc(t.acronym)}</td>` +
    `<td>${esc(t.release)}</td><td>${esc(t.outline_level)}</td>` +
    `<td>${esc(t.completion)}</td><td>${esc(t.start_date)}</td>` +
    `<td>${esc(t.finish_date)}</td><td>${esc(t.rapporteur_name)}</td>` +
    `<td class="ver">${esc(t.file_date)}</td></tr>`).join("") :
    '<tr><td colspan="10" class="empty">No items match.</td></tr>';
  const per = d.limit || st.per;
  const pages = Math.max(1, Math.ceil(d.total / per));
  const cur = Math.floor(st.offset / per) + 1;
  els.page.textContent = `Page ${cur} / ${pages}`;
  els.info.innerHTML = `<b>${d.total.toLocaleString()}</b> items`;
  els.prev.disabled = st.offset === 0;
  els.next.disabled = st.offset + (d.limit || st.per) >= d.total;
}

async function history(uid) {
  const d = await get("/api/history/" + encodeURIComponent(uid));
  els.dTitle.textContent = d.name || "Work item";
  els.dSub.textContent = d.unique_id + (d.acronym ? " — " + d.acronym : "");
  const L = d.latest || {};
  const kv = (k, v) => v ? `<div><small>${k}</small>${esc(v)}</div>` : "";
  els.dBody.innerHTML =
    (L.status_report ? `<p><span class="mut">Status:</span> ${esc(L.status_report)}</p>` : "") +
    (L.notes ? `<p><span class="mut">Notes:</span> ${esc(L.notes)}</p>` : "") +
    `<div class="kv">` +
    kv("Release", L.release) + kv("Level", L.outline_level) +
    kv("Completion", L.completion) + kv("Start", L.start_date) +
    kv("Finish", L.finish_date) + kv("Rapporteur", L.rapporteur_name) +
    kv("Email", L.rapporteur_email) + kv("WID", L.hyperlink) +
    kv("Latest snapshot", (L.file_date || "") + " / " + (L.source_file || "")) +
    `</div>`;
  els.dHist.innerHTML = d.history.map((h) =>
    `<tr><td class="ver">${esc(h.file_date)}</td><td>${esc(h.name)}</td>` +
    `<td>${esc(h.release)}</td><td>${esc(h.outline_level)}</td>` +
    `<td>${esc(h.completion)}</td><td>${esc(h.start_date)}</td>` +
    `<td>${esc(h.finish_date)}</td><td>${esc(h.status_report)}</td></tr>`).join("");
  els.detail.classList.add("open");
  els.overlay.classList.remove("hidden");
}
function close() { els.detail.classList.remove("open"); els.overlay.classList.add("hidden"); }

let t;
els.q.addEventListener("input", () => { clearTimeout(t);
  t = setTimeout(() => { st.q = els.q.value.trim(); st.offset = 0;
    load().catch((e) => say(e.message)); }, 250); });
function relLabel() { els.relLabel.textContent = st.releases.length
  ? (st.releases.length === 1 ? st.releases[0] : st.releases.length + " selected")
  : "All releases"; }
els.relList.addEventListener("change", () => {
  st.releases = [...els.relList.querySelectorAll("input:checked")].map((c) => c.value);
  relLabel(); st.offset = 0; load().catch((e) => say(e.message)); });
document.addEventListener("click", (e) => {
  if (els.relDrop.open && !e.target.closest("#relDrop")) els.relDrop.open = false; });
els.verFrom.addEventListener("change", () => { st.verFrom = els.verFrom.value;
  st.offset = 0; load().catch((e) => say(e.message)); });
els.verTo.addEventListener("change", () => { st.verTo = els.verTo.value;
  st.offset = 0; load().catch((e) => say(e.message)); });
els.sort.addEventListener("change", () => { st.sort = els.sort.value;
  st.offset = 0; load().catch((e) => say(e.message)); });
els.ord.addEventListener("change", () => { st.order = els.ord.value;
  st.offset = 0; load().catch((e) => say(e.message)); });
els.prev.addEventListener("click", () => { st.offset = Math.max(0, st.offset - st.per);
  load().catch((e) => say(e.message)); });
els.next.addEventListener("click", () => { st.offset += st.per;
  load().catch((e) => say(e.message)); });
els.per.addEventListener("change", () => { st.per = parseInt(els.per.value, 10) || 50;
  st.offset = 0; load().catch((e) => say(e.message)); });
els.reset.addEventListener("click", () => { st.q = ""; st.releases = [];
  st.verFrom = ""; st.verTo = "";
  st.sort = "unique_id"; st.order = "asc"; st.offset = 0; st.per = 50;
  els.q.value = ""; els.verFrom.value = ""; els.verTo.value = "";
  els.sort.value = "unique_id"; els.ord.value = "asc"; els.per.value = "50";
  els.relList.querySelectorAll("input:checked").forEach((c) => (c.checked = false));
  relLabel(); load().catch((e) => say(e.message)); });
els.dClose.addEventListener("click", close);
els.overlay.addEventListener("click", close);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
document.addEventListener("click", (e) => { const tr = e.target.closest("tr[data-uid]");
  if (tr) history(tr.dataset.uid).catch((e) => say(e.message)); });

(async function init() {
  try {
    const m = await get("/api/meta");
    els.meta.textContent =
      `Latest known state of ${m.items.toLocaleString()} work items ` +
      `(${m.rows.toLocaleString()} versioned rows, ${m.files} snapshots, ` +
      `${m.earliest} to ${m.latest}).`;
    const f = await get("/api/releases");
    els.relList.innerHTML = f.releases.map((r) =>
      `<label><input type="checkbox" value="${esc(r)}" />${esc(r)}</label>`).join("");
    (m.dates || []).forEach((d) => {
      for (const sel of [els.verFrom, els.verTo]) {
        const o = document.createElement("option"); o.value = d; o.textContent = d;
        sel.appendChild(o); } });
    await load();
    els.q.focus();
  } catch (e) { say("Init: " + e.message); }
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("PORT", 5917))
    except (TypeError, ValueError):
        port = 5917
    print(f"3GPP Work Plan -> http://{host}:{port}")
    app.run(host=host, port=port, debug=False)

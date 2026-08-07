# 3GPP Work Plan Explorer (Web App)

A Flask + vanilla-JS web app that lets you explore the archived 3GPP work plan
snapshots stored in `threegpp.sqlite3` (2016–2026, ~84 dated snapshots).

## Run

```bash
pip install -r requirements.txt   # includes flask
python3 app.py                    # serves http://127.0.0.1:5000
```

## Features

- **Latest work plan** — shows the most recent snapshot (default view).
- **Search / filter / sort** — free-text search across name, acronym, IDs,
  status report, notes, rapporteur; filters for Release, Outline level,
  Rapporteur, Acronym and a Completion-% range; sort by name, acronym, IDs,
  release, level, completion, start/finish date (asc/desc).
- **Snapshot as of a date/time** — enter any date/time and the app resolves to
  the latest snapshot whose `file_date <=` that moment (the work plan as it
  stood then). Dates before the earliest snapshot are clamped.
- **All items that exist at a moment** — whenever a snapshot (or date/time) is
  selected, the table shows every work plan item that existed at that moment,
  using the **latest version** of each item as of the cutoff — a point-in-time
  reconstruction across the whole snapshot history, not just the rows present
  in that single file. Searching / filtering / sorting all apply to this
  reconstructed set, and the snapshot badge shows its total item count. A
  dropdown also lists every snapshot directly.
- **Item history** — click any row to open a drawer with the item's details
  plus its full history across all snapshots (keyed on the stable `unique_id`),
  including a completion-% timeline.

## API

| Endpoint | Description |
| --- | --- |
| `GET /api/snapshots` | All snapshots `{file_date, source_file, row_count}` |
| `GET /api/resolve-snapshot?datetime=…` | Snapshot applying at a moment (default latest) |
| `GET /api/facets?snapshot=…` | Distinct releases / levels / rapporteurs / acronyms |
| `GET /api/items?snapshot=&q=&release=&outline_level=&rapporteur=&acronym=&completion_min=&completion_max=&sort=&order=&limit=&offset=` | Search/filter/sort/paginate items in a snapshot |
| `GET /api/items/<unique_id>` | Item details + cross-snapshot history |

`app.py` opens the database **read-only**; it never writes to
`threegpp.sqlite3`. Data is populated separately by `ingest.py`.

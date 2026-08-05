# 3GPP Work Plan — Long-Living DB Table Schema

## Design Rationale

The 85 xlsx files are periodic snapshots of the same evolving work plan.
Two approaches:

  A) Snapshot-per-file: one row per (file, work item). Preserves full
     history — you can reconstruct any past state. Cost: ~40x more rows
     (each work item appears in every file that contains it).

  B) Latest-state-only: one row per work item, overwritten on each ingest.
     Simpler, but loses history.

Recommendation: A (snapshot-per-file). The files ARE the history; throwing
that away defeats the purpose of archiving 85 snapshots. The table is
still modest — ~85 files x ~2000 rows/file ≈ 170K rows.

---

## Table: work_plan_items

```sql
CREATE TABLE work_plan_items (
    -- Metadata (which file this row came from)
    id              SERIAL PRIMARY KEY,
    source_file     TEXT    NOT NULL,          -- e.g. "Work_plan_3gpp_250930.xlsx"
    file_date       DATE    NOT NULL,          -- parsed from filename (2025-09-30)

    -- The 22 canonical columns
    item_id         TEXT,                      -- [0] ID (int in old files, str in new)
    unique_id       TEXT,                      -- [1] Unique_ID (0 = top-level, N = child task)
    name            TEXT    NOT NULL,          -- [2] Name
    acronym         TEXT,                      -- [3] Acronym
    outline_level   SMALLINT,                   -- [4] Outline_Level (1=top, 2=feature, 3=sub-feature...)
    release         TEXT,                      -- [5] Release ("Rel-16", "Rel-20", ...)
    resource_names  TEXT,                      -- [6] Resource_Names
    start_date      TIMESTAMP,                  -- [7] Start / Start_Date
    finish_date     TIMESTAMP,                  -- [8] Finish / Finish_Date
    completion      SMALLINT,                   -- [9] Percent_Complete / %Complete / Completion (0-100)
    hyperlink       TEXT,                       -- [10] Hyperlink (URL to 3gpp doc)
    status_report   TEXT,                       -- [11] Status_Report (incl. 161220 "NEW FIELD: WI Summary")
    rapporteur_name TEXT,                       -- [12] WI_rapporteur_name
    rapporteur_email TEXT,                      -- [13] WI_rapporteur_e_mail
    notes           TEXT,                       -- [14] Notes
    impacted_specs  TEXT,                       -- [15] Impacted_TSs_and_TRs (free text, can be very long)
    tsg_approved    DATE,                       -- [16] TSG_approved (date TSG approved the WI)
    pcg_approved    DATE,                       -- [17] PCG_approved
    tsg_stopped     DATE,                       -- [18] TSG_stopped (date TSG stopped the WI)
    pcg_stopped     DATE,                       -- [19] PCG_stopped
    created         TIMESTAMP,                  -- [20] Created
    last_modified   TIMESTAMP,                  -- [21] last_modif

    -- Ingest metadata
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## Indexes

```sql
-- Primary lookup: "show me the work plan as of file X"
CREATE INDEX idx_wpi_source_file ON work_plan_items (source_file);

-- "show me work item N across all snapshots"
CREATE INDEX idx_wpi_item_id_file ON work_plan_items (item_id, file_date);

-- "all items in release R as of file F"
CREATE INDEX idx_wpi_release_file ON work_plan_items (release, file_date);

-- "all items approved by TSG between date X and Y"
CREATE INDEX idx_wpi_tsg_approved ON work_plan_items (tsg_approved) WHERE tsg_approved IS NOT NULL;
```

---

## Type Decisions and Why

### item_id, unique_id: TEXT not INTEGER
Old files store these as Python int; new files (250930+) store as str ("1", "2").
Casting to TEXT unifies both. Unique_ID can be 0 (top-level) or N (sub-task N).

### outline_level: SMALLINT
Always a small integer (1-5). Old files: int; new files: str ("1").
Parse to SMALLINT on ingest; reject if > 32767.

### start_date, finish_date: TIMESTAMP
Excel stores these as datetime with a time component (08:00, 17:00).
Older files: Python datetime objects; newer files: same.
Kept as TIMESTAMP to preserve the time-of-day. If time is irrelevant, DATE works too.

### completion: SMALLINT (0-100)
Always an integer in the samples. SMALLINT is sufficient.
Named "completion" after the latest-era column name (the stable, current name).

### tsg_approved, pcg_approved, tsg_stopped, pcg_stopped: DATE
These columns are empty in most rows (only populated when a TSG/PCG action occurred).
The column names imply dates (the date TSG/PCG approved or stopped the work item).
Sample data showed them empty; typed as DATE to match the semantic meaning.
If they turn out to contain text (meeting number rather than date), change to TEXT.

### created, last_modified: TIMESTAMP
Old files: datetime objects. New files (250930+): strings like "Thu 15/01/15"
(a dd/yy/mm format that needs parsing) or "NA" (→ NULL).
The inconsistency means ingest must parse both formats.

### impacted_specs, status_report, notes, name: TEXT (unbounded)
These are free-text columns. "Impacted_TSs_and_TRs" can list many specs
and grow very long. Don't constrain length.

### hyperlink: TEXT
Stores URLs (to 3gpp documents) or "-" (→ NULL) or empty (→ NULL).

---

## Ingest Normalization Rules

On loading each xlsx file, apply these transformations:

  1. Sentinel values → NULL
     "-", "NA", "" (empty string), None  →  NULL
     Apply to ALL columns except item_id and name.

  2. Type coercion (per column)
     item_id          : str(value)                    (int or str in source → always str)
     unique_id        : str(value)
     outline_level    : int(value)                    (str "1" → 1)
     start_date       : value as-is if datetime, else parse
     finish_date      : value as-is if datetime, else parse
     completion       : int(value)
     created          : if datetime, keep; if str "Thu 15/01/15", parse;
                        if "NA" or empty, NULL
     last_modified    : same as created
     tsg_approved etc : if datetime, extract .date(); if str, parse; else NULL
     everything else  : str(value).strip() or NULL

  3. Column matching (from column_analysis.md)
     Tier 1: exact match (case-insensitive) on 9 invariant columns
     Tier 2: lowercase + includes on 13 variable columns

  4. Row filtering (see "Row Filtering" section below)
     Only rows with nonzero unique_id are inserted; all others skipped.

  5. file_date extraction from filename
     Pattern YYYY_MM_DD  →  date(YYYY, MM, DD)
     Pattern YYMMDD      →  date(2000+YY, MM, DD)  (YY < 80 → 20YY, else 19YY)

---

## Row Filtering (During Ingest)

Not every row in the xlsx files is a work item. Only actual work plan items
should be inserted into the table; non-work-item rows are skipped entirely.

### Primary discriminator: unique_id (column 1)

  unique_id is None or 0  →  skip (not a work item)
  unique_id is nonzero    →  insert (work item or sub-task)

Work items always have a 6-7 digit Unique_ID (e.g. 1050110, 810017).
Non-work-item rows have Unique_ID = 0 or empty.

### Secondary signal (backup check): acronym

  acronym = "-" (literal hyphen)  →  likely not a work item
  Every actual work item has a real acronym (FS_6G_REQ, 5GSAT_Ph4, ...).

### Skipped row types (for reference)

  file_header      — "Work Plan 3GPP version <date>", "Includes the WIDs
                     proposed at TSG#N", "Next main milestones: ...",
                     or empty rows at the top of the file.

  release_divider  — "--- RELEASE 20 ---"

  group_header     — Section headers like "Rel-20 6G Studies",
                     "Release 17 other Features". No Unique_ID, no dates.

### Pseudocode

```
function should_insert(unique_id):
    if unique_id is None or unique_id == 0:
        return false   # skip non-work-item rows
    return true
```

### Note on Outline_Level within work items

  Outline_Level 1 = work item (the feature/study item itself)
  Outline_Level 2 = sub-task (e.g. "Core part:", "Perf. part:", "Stage 1 for...")
  Both are inserted — they are all real work plan rows with nonzero Unique_IDs.

---

## Optional: work_plan_files (audit table)

Track which files have been ingested, to support idempotent re-ingest:

```sql
CREATE TABLE work_plan_files (
    source_file     TEXT PRIMARY KEY,
    file_date       DATE NOT NULL,
    row_count       INTEGER NOT NULL,
    header_json     JSONB NOT NULL,           -- raw header row for provenance
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## Querying the Latest State

```sql
-- Get the most recent snapshot's work items
SELECT *
FROM work_plan_items
WHERE source_file = (
    SELECT source_file
    FROM work_plan_items
    ORDER BY file_date DESC
    LIMIT 1
);
```

## Querying a Work Item's History

```sql
-- Track WI "123" across all snapshots
SELECT file_date, name, completion, start_date, finish_date, tsg_approved
FROM work_plan_items
WHERE item_id = '123'
ORDER BY file_date;
```

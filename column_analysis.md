# 3GPP Work Plan — Column Analysis

Derived from 85 xlsx files (2016-11-10 to 2026-06-26), 7 unique headers.
See headers.txt for the raw header-by-header listing.

---

## Literally Invariant Columns (9)

Case-insensitive identical string in every file:

1. ID
2. Unique_ID
3. Name
4. Acronym
5. Release
6. Hyperlink
7. Notes
8. Created
9. last_modif

---

## Semantically Invariant Columns (13)

Same meaning in every file; name changed over time.

### 1. Outline_Level

    161110 – 170106 : Outline_Level
    190315 – 220322 : Outline Level        (underscores -> spaces)
    220621          : OutlineLevel         (spaces removed)
    250930 – present : Outline_Level        (underscores restored)

### 2. Resource_Names

    161110 – 170106 : Resource_Names
    190315 – 220322 : Resource Names
    220621          : ResourceNames
    250930 – present : Resource_Names

### 3. Start

    161110 – 170106 : Start_Date
    190315 – present : Start                (suffix _Date dropped permanently)

### 4. Finish

    161110 – 170106 : Finish_Date
    190315 – present : Finish

### 5. Percent_Complete / Completion

    161110 – 170106 : Percent_Complete
    190315          : % Complete
    220322 – 220621 : %Complete             (space removed)
    250930 – present : Completion           (fully renamed)

### 6. Status_Report

    161110          : Status_Report
    161220          : NEW FIELD: WI Summary (one-off replacement, reverted next file)
    170106 – 170106 : Status_Report         (restored)
    190315 – 220621 : Status Report         (underscores -> spaces)
    250930 – present : Status_Report        (underscores restored)

### 7. WI_rapporteur_name

    161110 – 170106 : WI_rapporteur_name
    190315 – 220621 : WI rapporteur name
    250930 – present : WI_rapporteur_name

### 8. WI_rapporteur_e_mail

    161110 – 170106 : WI_rapporteur_e_mail
    190315 – 220621 : WI rapporteur e-mail  (underscore->space, e_mail->e-mail)
    250930 – present : WI_rapporteur_e_mail

### 9. Impacted_TSs_and_TRs

    161110 – 170106 : Impacted_TSs_and_TRs
    190315 – 220621 : Impacted TSs and TRs
    250930 – present : Impacted_TSs_and_TRs

### 10. TSG_approved

    161110 – 170106 : TSG_approved
    190315 – 220621 : TSG approved
    250930 – present : TSG_approved

### 11. PCG_approved

    161110 – 170106 : PCG_approved
    190315 – 220621 : PCG approved
    250930 – present : PCG_approved

### 12. TSG_stopped

    161110 – 170106 : TSG_stopped
    190315 – 220621 : TSG stopped
    250930 – present : TSG_stopped

### 13. PCG_stopped

    161110 – 170106 : PCG_stopped
    190315 – 220621 : PCG stopped
    250930 – present : PCG_stopped

---

## Overlapping Pattern

    161110 – 170106 : underscore_style  (original era)
    190315 – 220621 : space style       (rename era)
    250930 – present : underscore_style  (revert era)

Two permanent structural changes survive the revert:
  - Start_Date / Finish_Date  ->  Start / Finish   (suffix dropped from 190315 onward)
  - Percent_Complete          ->  Completion       (fully renamed from 250930 onward)

One one-off anomaly:
  - 161220 Status_Report temporarily renamed to "NEW FIELD: WI Summary", reverted in 170106.

---

## Detection / Matching Strategy

Two-tier approach. All 22 columns are always present in the same order;
no insertions or deletions across 85 files.

### Tier 1 — Exact match (case-insensitive) on the 9 invariant columns

    ID, Unique_ID, Name, Acronym, Release, Hyperlink, Notes, Created, last_modif

These are literally identical in every file. Match them first.
They also act as positional anchors that scope the Tier 2 tests:
the variable columns are slotted into the gaps between anchors.

### Tier 2 — lowercase + includes on the 13 variable columns

Each column keyed by a unique substring that no other column
(invariant or variable) contains. Scoped to the positional gap
between anchors so collisions (e.g. includes("name") matching both
Name and WI_rapporteur_name) never arise.

    Outline_Level        includes("outline")
    Resource_Names       includes("resource")
    Start                includes("start")
    Finish               includes("finish")
    Percent_Complete     includes("complet")          // catches percent_complete, % complete, %complete, completion
    Status_Report        includes("status") || includes("summary")   // 161220 one-off: "NEW FIELD: WI Summary"
    WI_rapporteur_name   includes("rapporteur") && includes("name")
    WI_rapporteur_email  includes("rapporteur") && includes("mail")
    Impacted_TSs_and_TRs includes("impacted")
    TSG_approved         includes("tsg") && includes("approv")
    PCG_approved         includes("pcg") && includes("approv")
    TSG_stopped          includes("tsg") && includes("stop")
    PCG_stopped          includes("pcg") && includes("stop")

### Why this suffices

  - The 9 invariant columns are exact-matched, consuming their slots.
  - The 13 variable columns each have a unique keyword
    (outline, resource, start, finish, complet, status/summary,
    impacted, rapporteur, tsg, pcg) that no other column shares.
  - The one-off 161220 "NEW FIELD: WI Summary" is handled by the
    single OR fallback on Status_Report.
  - "complet" as a stem catches all four variants:
    Percent_Complete, % Complete, %Complete, Completion.
  - No normalization, fuzzy distance, fingerprinting, or assignment
    algorithm needed — the column set is small and stable enough
    that keyword presence is a perfect discriminator.

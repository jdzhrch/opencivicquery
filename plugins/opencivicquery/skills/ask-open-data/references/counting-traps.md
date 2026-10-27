# Counting traps in civic datasets

These traps recur across portals. Use them to question a dataset that no source pack documents. Each one is a reason to read the agency's dataset page before you count, and none of them replaces it.

## The row is narrower than the thing in the question

- **Rows that are violations.** Inspection datasets often hold one row per violation cited. Counting rows overstates inspections and establishments. Count distinct establishment IDs, and count inspections by grouping on establishment, date, and inspection type.
- **Rows that repeat per offense code.** Police datasets often repeat one report once per offense code. Count distinct report or incident IDs.
- **Rows that are transactions.** Campaign finance rows can be amendments, refunds, loans, or several schedules describing the same money. Filter by form or transaction type before you add anything up.
- **Rows that are locations.** Business registries often list each location of one account as its own row.

## The record and the event differ

- **Notices, filings, and complaints record a step.** An eviction notice does not show that an eviction happened. A 311 request is not a confirmed condition. Several people can report the same problem.
- **Executed evictions are the last step.** A dataset of executed evictions leaves out every case that ended earlier.
- **Permits are not construction.** Filing, approval, issuance, and completion are separate dates and separate events.

## The dataset holds part of the history

- **Active-only datasets.** Some datasets keep only currently active entities. Closed restaurants or businesses drop out with their history, so earlier years undercount.
- **Rolling windows.** Some keep only the last few years, or only years since a system changed.
- **Split datasets.** One subject can live in several datasets by era, for example 311 data before and after a system migration.
- **Revised records.** Current rows can change after publication. The snapshot freezes what the agency returned at query time.

## The place and the time are choices

- **Several boundaries share one name.** A neighborhood name can mean a planning area, a police district, a council district, or a ZIP code. Say which one you used.
- **Several dates share one record.** Created, closed, filed, and updated dates answer different questions. Name the one the window applies to.
- **Category labels change.** Agencies rename and split complaint types, which can break a long series.

## Scores and grades follow rules the data does not hold

Point values, grade bands, and severity classes come from the agency's rules. When a value looks inconsistent, for example a grade that does not match its score, check the agency's documentation, report the inconsistency, and add a question for the agency. Never derive a grade or a severity from a number yourself.

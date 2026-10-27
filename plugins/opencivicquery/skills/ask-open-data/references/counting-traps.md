# Counting traps in civic datasets

These are patterns that recur across portals. Each one is something to check, and none of them is a fact about any particular dataset. Use them to question a dataset that no source pack documents, and read the agency's dataset page before you count.

When one of these traps matters to an answer, say that it may apply. Say it applies only when the agency's documentation or a source pack confirms it, and cite which. Otherwise add it to the questions for the agency.

## The row may be narrower than the thing in the question

- **Rows that are violations.** If one row is one violation cited, counting rows overstates inspections and establishments. Check the row grain. If it holds, count establishments with a distinct establishment ID.
- **Rows that repeat per offense code.** If one report appears once per offense code, counting rows overstates reports. Check the row grain. If it holds, count distinct report or incident IDs.
- **Rows that are transactions.** If rows mix contributions, loans, refunds, amendments, or several schedules, a plain sum mixes different money. Check which form or transaction types the dataset holds, and filter before you add anything up.
- **Rows that are locations.** If each location of one account is its own row, counting rows overstates businesses. Check for an account identifier.

## The record may differ from the event

- **Notices, filings, and complaints.** If a dataset records a notice, a filing, or a complaint, a count of rows is a count of those records. Report it in that unit, and do not call it a count of outcomes or confirmed conditions.
- **The last step of a process.** If a dataset records only the final step, such as executed evictions, it does not show the earlier steps. Say which step the rows record.
- **Permits and other multi-step records.** If a record has several date fields, such as filed, issued, and completed, each one marks a different event. Name the one your time window uses.

## The dataset may hold part of the history

- **Active-only datasets.** If the documentation says the dataset keeps only currently active entities, records of closed ones may be missing, which would affect comparisons across years. Check the documentation, and ask the agency how closed entities are handled.
- **Rolling windows.** If the documentation says the dataset keeps only recent years, earlier periods are not in it. Check the coverage dates before you compare periods.
- **Split datasets.** If the documentation names a separate dataset for another era, a long series needs both. Check the dataset description for links to related datasets.
- **Revised records.** If the documentation says values change after publication, today's count can differ from tomorrow's. The snapshot freezes what the agency returned at query time.

## The place and the time are choices

- **Several boundaries can share one name.** A neighborhood name can mean a planning area, a police district, a council district, or a ZIP code. Say which field you used.
- **Several dates can share one record.** Created, closed, filed, and updated dates answer different questions. Name the one the window applies to.
- **Category labels can change.** If the documentation says categories are added or renamed, check that the category you filter on covers the whole period.

## Scores and grades follow rules the data may not hold

Point values, grade bands, and severity classes come from the agency's rules. When a value looks inconsistent, for example a grade that does not match its score, check the agency's documentation, report what the records show, and add a question for the agency. Never derive a grade or a severity from a number yourself.

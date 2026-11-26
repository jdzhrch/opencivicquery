# Legistar page fixtures

Recorded from `https://sfgov.legistar.com` on 2026-11-26 with plain requests under the
server's own identity, then trimmed. They are public records of the San Francisco Board of
Supervisors.

| File | Page | Request |
|---|---|---|
| `legislation_form.html` | `Legislation.aspx` | first GET, simple search |
| `legislation_advanced.html` | `Legislation.aspx` | POST of the "Advanced search" switch |
| `legislation_results.html` | `Legislation.aspx` | title `housing`, introduced 2026-10-01 to 2026-11-26 |
| `legislation_results_paged.html` | `Legislation.aspx` | introduced 2026-09-01 to 2026-11-26, 363 matches |
| `legislation_results_file.html` | `Legislation.aspx` | file/enactment # `260875` |
| `legislation_results_none.html` | `Legislation.aspx` | title `zzqqxxjj`, no match |
| `legislation_detail.html` | `LegislationDetail.aspx` | file 260875 |
| `meeting_detail.html` | `MeetingDetail.aspx` | Rules Committee, 2026-09-14 |
| `history_detail.html` | `HistoryDetail.aspx` | file 260875, Board of Supervisors, 2026-09-29 |
| `history_detail_no_votes.html` | `HistoryDetail.aspx` | file 260875, the mayor's approval, no roll call |

What the trimming changed:

- Scripts, styles, date-picker popups, navigation, sign-in links, attachment lists, and
  presentation attributes are removed.
- Hidden form values longer than 80 characters, such as `__VIEWSTATE`, read `TRIMMED`
  followed by their original length.
- Text longer than 160 characters ends in `[trimmed]`.
- Menus keep their first three entries.
- `legislation_results.html` keeps 4 of its 22 rows, and its count label reads "4 records"
  to match. `legislation_results_paged.html` keeps 3 of the 100 rows the page showed and
  leaves its count of 363 and its pager as served.

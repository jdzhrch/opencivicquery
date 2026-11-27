# Where else to look

The OpenCivicQuery and CourtListener tools reach free, public records that a machine may read. Many records a lead depends on sit elsewhere: behind a subscription, behind a per-page fee, or on a public website that refuses or forbids automated access. This file lists those channels so a reporter knows where a record may be and how to get it, and can tell a source which channel to check. For some websites reporters name, it also says which tool already reaches the same records.

Every entry comes from the pages named in its Source lines, checked on the date shown. Prices change. Coverage claims are the vendor's own.

## How to use this file

- **Use it after the tools come up short,** or when the reporter asks where else a record might be. Free records come first: run the tools before you point anywhere else.
- **When a reporter names a website, look for it here before you say the tools cannot reach it.** An entry with a **The tools reach** line names the tool or the dataset that holds the same records. Find a dataset with `search_datasets` and read it with `describe_dataset` before you query it. Answer from that source, say which one you used, and then say what stays out of reach.
- **Say that OpenCivicQuery did not search the channel.** Never say a channel holds a specific record. Say what kind of records it describes itself as holding.
- **For each channel, give** what it holds, how a reporter gets access with every limit the entry lists, the published price, the date the entry was checked, and the link. When a price comes from a dated document, give that document's date too. When the price is not published, say so.
- **Where an entry gives Steps, pass them on:** the page to open, what to type or select, and which file to download. A reporter can download a file and give it to you to read. Do not open these pages yourself.
- **List every entry that fits the record type.** Entry order is not a ranking, and a Claude connector is not a recommendation. Do not recommend one vendor over another.
- **Where an entry says a site allows search by hand,** say so together with that entry's access limits. Never suggest a way around a site's terms, a CAPTCHA, a login, or a block on programs.
- **Two entries look up a named person:** the San Francisco Superior Court case lookup and the Sheriff's jail custody lookup. Point to them only for a person the reporter already names in a public matter, never to find out about a private individual.
- **Where an entry suggests questions to ask,** pass them on with the agency's channel. For San Francisco agencies, the records-request and press channels are in [san-francisco-request-channels.md](san-francisco-request-channels.md).

## Federal court dockets and filings

CourtListener holds federal filings only when someone has added them to its RECAP Archive. These channels reach the rest.

### PACER
- **Holds:** dockets and documents from every federal district, bankruptcy, and appellate court, and a national case index.
- **Access:** a PACER account, for the registrant's own use.
- **Cost:** $0.10 a page, capped at $3 a document; transcripts have no cap; opinions are free; fees are waived when a user's charges are $30 or less in a quarter; searches are billed even when nothing matches. From 2027-01-01 the rate is $0.12 a page and the waiver $40.
- **Source:** https://pacer.uscourts.gov/pacer-pricing-how-fees-work (checked 2026-11-20, page)
- **Source:** https://www.uscourts.gov/sites/default/files/document/appendix_02_-_epa_fy2027.pdf (checked 2026-11-20, page)
- **Source:** https://pacer.uscourts.gov/policy-procedures (checked 2026-11-20, page)

### CourtListener RECAP Fetch
- **Holds:** any PACER docket or document the reporter buys through it, which it then adds to CourtListener.
- **Access:** the reporter's own CourtListener account and PACER login. The reporter still pays PACER.
- **Cost:** PACER's fees.
- **Source:** https://wiki.free.law/c/courtlistener/help/api/rest/v4/recap (checked 2026-11-20, page)

### Bloomberg Law
- **Holds:** federal and state court dockets, searchable by case, party, court, judge, filing, and keyword, with filing downloads and docket alerts. Has a Claude connector.
- **Access:** Bloomberg Law sign-in.
- **Cost:** not stated on the connector page.
- **Source:** https://claude.com/connectors/bloomberg-law (checked 2026-11-20, page)

### Midpage
- **Holds:** federal dockets retrieved from PACER on demand. Has a Claude connector.
- **Access:** a Midpage account.
- **Cost:** Professional $100 a month, Law Student $25 a month, with $25 a month in PACER credit per user and usage billing beyond it.
- **Source:** https://www.midpage.ai/pricing (checked 2026-11-20, page)
- **Source:** https://www.midpage.ai/blog/ask-your-ai-about-any-federal-case-midpage-now-retrieves-pacer-dockets-on-demand (checked 2026-11-20, page)

### Docket Alarm (part of Clio)
- **Holds:** court dockets, docket updates, case alerts, and documents.
- **Access:** a Docket Alarm account.
- **Cost:** free for 5 actions a week, without PACER documents; $39.99 a month plus per-use fees such as $4 a document; or $99 a user a month flat, with PACER and state court document fees billed separately.
- **Source:** https://www.docketalarm.com/pricing (checked 2026-11-20, page)

### PacerMonitor
- **Holds:** federal dockets and documents from PACER.
- **Access:** a PacerMonitor account. The Enterprise and API plans also need the reporter's PACER credentials.
- **Cost:** Plus $49 a month and 15 cents a page; Professional $99 a month and 10 cents a page; Enterprise $499 a month for 15 users; a free account pays $5 a document.
- **Source:** https://pacermonitor.com/pricing (checked 2026-11-20, page)

### U.S. District Court for the Northern District of California
- **Holds:** its dockets and filings, in PACER. Its website also publishes three free lists: a feed of new docket entries, the filings of the last seven days, and a Cases of Interest list. Their links lead into PACER.
- **Access:** a PACER account for dockets and documents; the three lists open without a login.
- **Cost:** PACER's fees, in the PACER entry above; the lists are free.
- **The tools reach:** the opinions and orders the court publishes as opinions, through `search_court_opinions` (GovInfo), and the dockets and filings in CourtListener's RECAP Archive, through CourtListener's connector. Filings nobody has added to RECAP stay in PACER.
- **Source:** https://ecf.cand.uscourts.gov/cgi-bin/rss_outside.pl (checked 2026-11-26, live)
- **Source:** https://cand.uscourts.gov/cases-e-filing/cases/recently-filed-case-documents (checked 2026-11-26, live)
- **Source:** https://cand.uscourts.gov/cases-e-filing/cases/cases-of-interest (checked 2026-11-26, live)

## State court records

NYSCEF's terms forbid automated access, and New York's eCourts sites block automated clients.

### Trellis
- **Holds:** state court dockets; its coverage table lists 44 states and the District of Columbia. Has a Claude connector.
- **Access:** a Trellis account. A free account works up to an unstated limit, then needs the paid plan.
- **Cost:** the paid plan (Lawyer Entry + Judge Analytics) is priced on Trellis's website.
- **Source:** https://support.trellis.law/trellis-law-mcp-technical-documentation (checked 2026-11-20, page)

### UniCourt
- **Holds:** cases from what it describes as more than 5,000 state and federal courts in 46 states.
- **Access:** a UniCourt account; its API only through an Enterprise agreement.
- **Cost:** Personal $59, Professional $199, Premium $399 a month; court document fees pass through.
- **Source:** https://unicourt.com/pricing (checked 2026-11-20, page)
- **Source:** https://visit.unicourt.com/court-data-api (checked 2026-11-20, page)

### judyrecords
- **Holds:** court cases; its Full-Text API covers what it describes as more than 750 million cases.
- **Access:** search on its website. Programs may use only its API, priced by its sales team.
- **Cost:** API price not published.
- **Source:** https://www.judyrecords.com/api (checked 2026-11-20, live)

### New York State courts
- **Holds:** NYSCEF holds documents e-filed in courts that take part in NYSCEF; WebCivil and the other eCourts services look up cases.
- **Access:** search by hand on NYSCEF and eCourts, both reached from nycourts.gov. NYSCEF's terms forbid bots and data mining, and both block automated clients.
- **Cost:** not stated.
- **Source:** https://iappscontent.courts.state.ny.us/NYSCEF/live/termsOfUse.htm (checked 2026-11-20, live)

### Texas: re:SearchTX
- **Holds:** public state court cases statewide.
- **Access:** registration. Registered members of the public do not see residential evictions filed in justice courts or guardianship cases, and they pay document fees.
- **Cost:** document fees apply; amounts not confirmed.
- **Source:** https://www.tarrantcountytx.gov/content/dam/main/county-clerk/TXSupremeCourtTechStandards2026.pdf (checked 2026-11-20, page)

### Illinois: re:SearchIL
- **Holds:** state court cases. The public may view documents only in arbitration, eminent domain, law, law magistrate, municipal corporation, and tax cases, plus reviewing courts. Criminal, eviction, guardianship, probate, and family documents are open remotely only to Illinois lawyers and legal services providers.
- **Access:** registration.
- **Cost:** $0.10 a page, up to $3 a document (policy revised effective 2022-06-01).
- **Source:** https://efile.illinoiscourts.gov/wp-content/uploads/2022/10/Remote_Access_Policy.pdf (checked 2026-11-20, page)

### Wisconsin: WCCA
- **Holds:** Wisconsin circuit court case records.
- **Access:** machine download through a paid agreement (form Rev. 08/2022) that forbids sharing the access and requires the subscriber to update anything it published within 7 days of new data.
- **Cost:** $12,500 for 12 months.
- **Source:** https://www.wicourts.gov/courts/resources/docs/RESTagreementpaid.pdf (checked 2026-11-20, live)

### Virginia Court Data
- **Holds:** an unofficial, anonymized bulk copy of Virginia circuit and general district court cases through 2024. It says journalists can request the full data.
- **Access:** download from its website.
- **Cost:** none shown.
- **Source:** https://virginiacourtdata.org/ (checked 2026-11-20, live)

### California: San Francisco Superior Court
- **Holds:** registers of actions for civil, family law, probate, and small claims cases filed from 1987 on, with many filed civil documents such as complaints and rulings; calendars and registers of actions for criminal cases; case calendars up to 120 days ahead; and tentative rulings.
- **Access:** search by hand. Every lookup sends the visitor through a CAPTCHA first, and the Judicial Council's terms forbid bots and data mining. Civil cases are found by case number or by a party's last and first name, criminal cases by case number or attorney name. An unlawful detainer (eviction) case needs the access code from the notice to defendants, or the names of one plaintiff and one defendant with the full premises address exactly as on the complaint. Criminal documents are not open remotely under California Rules of Court rule 2.503(c), and neither are family, juvenile, guardianship, mental health, or civil harassment records. Viewing a paper file at the courthouse requires leaving a government photo ID with the clerk.
- **Cost:** viewing online is free. Copies by request go through the court's GovQA portal (account required) at $0.50 a page. A file at least two years old may be stored off-site, with a $6 retrieval fee and at least 15 business days' notice.
- **Steps:** open https://sf.courts.ca.gov/online-services and choose Case Query. The civil lookup is https://webapps.sftc.org/ci/CaseInfo.dll and the criminal lookup is https://webapps.sftc.org/crimportal/crimportal.dll; both open on the CAPTCHA.
- **The tools reach:** no case records. CourtListener does not hold California trial court dockets. The court's criminal data dashboards give aggregate counts only.
- **Ask:** the court's media mailbox, in the request channels file. Questions worth sending: whether a case file exists that the online lookup does not show, and whether the court would provide calendars, registers of actions, and indexes in bulk, which rule 2.503(g) allows. The Civil Records Division takes records requests through GovQA and lists CCCRecordsDepartment@sftc.org and 415-551-3802.
- **Source:** https://sf.courts.ca.gov/online-services (checked 2026-11-26, page)
- **Source:** https://webapps.sftc.org/ci/CaseInfo.dll (checked 2026-11-26, live)
- **Source:** https://sf.courts.ca.gov/divisions/civil-division/civil-records (checked 2026-11-26, page)
- **Source:** https://sf.courts.ca.gov/system/files/news/criminal-portal-announcement.pdf (checked 2026-11-25, page)
- **Source:** https://www.courts.ca.gov/11529.htm (checked 2026-11-25, page)
- **Source:** https://sf.courts.ca.gov/divisions/criminal/criminal-data-dashboards (checked 2026-11-25, page)

## Laws, codes, and case law

### San Francisco Municipal Code (American Legal Publishing)
- **Holds:** the San Francisco Charter and the city's codes, such as the Planning, Administrative, Police, and Health codes.
- **Access:** read in a browser. Code pages answer programs with a challenge page.
- **Cost:** free in a browser.
- **Steps:** open https://codelibrary.amlegal.com/codes/san_francisco/latest/overview, pick the code, then the article and section, or search for a section number or a phrase.
- **The tools reach:** the ordinances that amend the code, in San Francisco's Legistar, through `search_legislation` with client sfgov. An ordinance shows the change, not the consolidated code.
- **Ask:** the Clerk of the Board of Supervisors, through the board's mailbox in the request channels file: whether an amendment has taken effect, and whether the online code includes it.
- **Source:** https://codelibrary.amlegal.com/codes/san_francisco/latest/overview (checked 2026-11-26, live)
- **Source:** https://codelibrary.amlegal.com/codes/san_francisco/latest/sf_planning/0-0-0-17829 (checked 2026-11-25, live)

### Justia
- **Holds:** republished court opinions, dockets, and state and federal codes, including the California codes.
- **Access:** read in a browser. Its pages refuse programs.
- **Cost:** free.
- **The tools reach:** federal court opinions, through `search_court_opinions` (GovInfo), and federal and state case law and federal dockets, through CourtListener's connector. No tool reaches state code text. The official California code text is on leginfo.legislature.ca.gov, which a reporter reads in a browser, and the California Supreme Court and Courts of Appeal post their published opinions as PDFs on courts.ca.gov.
- **Source:** https://law.justia.com/codes/california/code-pen/part-1/title-11-5/section-422/ (checked 2026-11-26, live)
- **Source:** https://leginfo.legislature.ca.gov/ (checked 2026-11-26, live)
- **Source:** https://courts.ca.gov/opinions/publishedcitable-opinions (checked 2026-11-26, live)

### FindLaw
- **Holds:** republished state codes, federal and state court opinions, and plain-language legal explainers.
- **Access:** read in a browser. Its pages refuse plain program requests.
- **Cost:** free.
- **The tools reach:** the same as for Justia: GovInfo for federal opinions, and CourtListener for case law and federal dockets. FindLaw's code pages can lag the official text: a California Penal Code page was marked current as of 2026-01-01 when checked. Quote code text from leginfo.legislature.ca.gov. The explainers are not records, and no tool replaces them.
- **Source:** https://codes.findlaw.com/ca/penal-code/pen-sect-422/ (checked 2026-11-25, page)

## Immigration court

### TRAC Immigration
- **Holds:** immigration court and enforcement data tools: new proceedings, outcomes, backlog, representation, asylum and bond decisions, asylum decisions by judge, and ICE detention, detainers, and facilities. Each tool shows how recent its data is; some stopped updating between 2017 and 2024. The San Francisco immigration court is one court in the filters. TRAC builds its figures from EOIR records it obtains under FOIA.
- **Access:** a reporter reads it directly. The immigration tools opened without a login when checked. Its terms forbid bots, scripts, and scraping, bar building a database from its data, and bar exposing TRAC data through an AI system, so never fetch it. Reproducing a table or graph requires crediting TRAC with its full web address.
- **Cost:** opening the immigration tools is free. Paid: TRAC's fee schedule lists licenses for TRACFed and its Judge Information Center, and its subscription for news organizations costs $470 one-time setup, then a yearly fee by daily circulation: up to 150,000 is $1,465; 150,001 to 300,000 is $2,225; 300,001 to 600,000 is $3,120; 600,001 to 900,000 is $4,370. The pages checked do not say which immigration tools, if any, need a paid license.
- **Steps:** open https://tracreports.org/immigration/tools/, pick the tool that fits the question, then filter by state, court, hearing location, or nationality.
- **The tools reach:** nothing. For one person's case status, see EOIR's ACIS below.
- **Ask:** the Executive Office for Immigration Review for official counts, and TRAC for how it computed a figure.
- **Source:** https://tracreports.org/immigration/tools/ (checked 2026-11-26, page)
- **Source:** https://tracreports.org/phptools/immigration/backlog/ (checked 2026-11-25, live)
- **Source:** https://tracreports.org/notices/fee_schedule.html (checked 2026-11-25, page)
- **Source:** https://tracreports.org/notices/fee_site.html (checked 2026-11-20, page)
- **Source:** https://tracreports.org/terms/ (checked 2026-11-25, page)

### EOIR case status (ACIS)
- **Holds:** the status of one immigration court case.
- **Access:** lookup by hand with the person's A-number and nationality; the site uses a CAPTCHA.
- **Cost:** free.
- **Source:** https://acis.eoir.justice.gov/en/ (checked 2026-11-20, live)

## Elections and campaign finance

### San Francisco Department of Elections results
- **Holds:** official results for San Francisco elections back to 1995. For recent elections the reports include a Statement of the Vote by precinct, district, and neighborhood, in PDF and Excel, and a Cast Vote Record of ballot-level data, in JSON. The rounds of ranked-choice contests for city offices are published on the same election pages. File sets for older elections were not checked.
- **Access:** open by hand in a browser; the election pages refuse programs. Election-night and canvass reports are preliminary, and the final report is the certified one.
- **Cost:** free, no login.
- **Steps:** open https://www.sf.gov/results, which lists certified results by election date from 2026 back to 1995, and follow the election's link to its page on sfelections.org, such as https://www.sfelections.org/results/20241105w/index.html for November 5, 2024. The address pattern changes by year, so start from the list. For results by precinct, district, or neighborhood, download the Statement of the Vote in Excel. For ballot-level analysis, download the Cast Vote Record. For the rounds of a ranked-choice contest, use the ranked-choice results on the same election page. State and federal contests in San Francisco also appear in the California Secretary of State's Statement of Vote, a free official source with county tables in PDF and XLSX for each statewide election; it has no city contests.
- **The tools reach:** no results. DataSF holds precinct boundaries and a history of ballot measures since 1907.
- **Ask:** the department lists no media contact; its general mailbox, in the request channels file, takes records requests. Questions worth sending: whether past elections have the same precinct files, when results for a contest are final, and whether the department would publish its results files where programs may read them.
- **Source:** https://www.sf.gov/results (checked 2026-11-26, page)
- **Source:** https://www.sf.gov/november-5-2024-presidential-general-election-results-reporting-schedule (checked 2026-11-26, page)
- **Source:** https://www.sfelections.org/results/20241105w/index.html (checked 2026-11-26, live)
- **Source:** https://www.sos.ca.gov/elections/prior-elections/statewide-election-results (checked 2026-11-26, page)

### Ballotpedia
- **Holds:** encyclopedia pages on candidates, officeholders, elections, results, and ballot measures across the country, republished from official records.
- **Access:** read in a browser; the site refuses programs. Its data interface needs an API key from a paid package.
- **Cost:** free to read. The data interface's price is not published.
- **Steps:** open https://ballotpedia.org/San_Francisco,_California, or search the site for the candidate, the office, or the measure.
- **The tools reach:** for San Francisco candidates and committees, the Ethics Commission's campaign finance datasets on DataSF; see the Ethics Commission entry below. Official results come from the Department of Elections entry above.
- **Ask:** Ballotpedia is not an agency. Confirm a fact taken from it with the agency that produced the record, usually the Department of Elections or the Ethics Commission.
- **Source:** https://ballotpedia.org/San_Francisco,_California (checked 2026-11-26, live)
- **Source:** https://developer.ballotpedia.org/ (checked 2026-11-25, page)

### San Francisco Ethics Commission website
- **Holds:** the commission's press releases, audits, reports, agendas and minutes, enforcement settlements, and dashboards of campaign finance data.
- **Access:** its website, free. OpenCivicQuery does not read it.
- **Cost:** free.
- **The tools reach:** most of its data, on DataSF through `query_dataset`. Campaign finance: "Campaign Finance - Transactions", "Campaign Finance - Summary Totals", "Campaign Finance - Filings Received by SFEC", "Campaign Finance - San Francisco Campaign Filers", and "Campaign Finance - Public Funds Approved"; the commission's dashboards draw on these datasets. Lobbyists and campaign consultants: the commission's lobbyist and campaign consultant datasets. Fines: "Ethics Commission Enforcement Penalties Issued", one row per case with its date, respondents, violations, penalty, and a link to the settlement PDF. "Campaign Finance - Filers With Delinquent Campaign Filings" stopped updating in 2022. A search of all portals also returns "Campaign Finance Penalty Notices", which is Oregon's; never use it for San Francisco. The text of press releases, audits, minutes, and settlement PDFs stays out of reach: give the reporter the website and the settlement link from the dataset row.
- **Ask:** the commission lists no media contact; records requests go through the city's portal, in the request channels file.
- **Source:** https://sfethics.org/ (checked 2026-11-26, live)
- **Source:** https://sfethics.org/disclosures/campaign-finance-disclosure/campaign-finance-disclosure-data (checked 2026-11-26, page)
- **Source:** https://sfethics.org/disclosures/lobbyist-disclosure/lobbyist-disclosure-data (checked 2026-11-26, page)

## Property, permits, and planning

### San Francisco Assessor-Recorder recorded documents
- **Holds:** recorded documents such as deeds, deeds of trust, reconveyances, and liens, from 1990 on, indexed by party name, document number, document type, and Assessor's Parcel Number (block and lot). It does not search by street address. Records from before January 1, 1990 can be searched only at City Hall, room 190.
- **Access:** the search opens behind a disclaimer the visitor must accept. Viewing documents online needs a registered account with ID verification.
- **Cost:** viewing online is free after registration. A guest can buy an online copy for $1.81 a document plus a $2 card service fee. In person or by mail, a copy is $9.70 for the first page and $0.39 for each further page; certification adds $1.00.
- **Steps:** open https://recorder.sfgov.org/ and search by name, document number, document type, or block and lot.
- **The tools reach:** DataSF, to turn an address into a block and lot first: the parcels dataset of active and retired parcels (address ranges, block, lot) and "Assessor Historical Secured Property Tax Rolls" (property location, parcel number, assessed values, and the last sale date on the roll). The roll shows values and a sale date, not the owner or the recorded documents.
- **Ask:** the Office of the Assessor-Recorder, assessor@sfgov.org or 628-652-8100. Questions worth sending: whether the index of names and dates can be searched without registration, and whether the office offers a bulk index or a data subscription.
- **Source:** https://recorder.sfgov.org/ (checked 2026-11-26, live)
- **Source:** https://www.sf.gov/get-copies-recorded-documents (checked 2026-11-26, page)

### Accela Citizen Access (San Francisco Planning)
- **Holds:** San Francisco Planning Department records, each with its attachments, such as plans and letters, and its processing timeline.
- **Access:** record pages and the Planning search open in a browser without a login. Whether every attachment opens without a login was not checked.
- **Cost:** free.
- **Steps:** open https://aca-prod.accela.com/ccsf/Cap/CapHome.aspx?module=Planning&TabName=Planning and search for the record.
- **The tools reach:** the records themselves, on DataSF through `query_dataset`: "Planning Department Records - Projects" and "Planning Department Records - Non-Projects", with record type, description, status, address, block and lot, open and close dates, and assigned planner. Building permits are not in this Accela system: use the DataSF datasets "Building Permits" and "PermitSF Permitting Data". Attachments and timelines stay out of reach: give the reporter the record's page.
- **Ask:** the Planning Department's press office, in the request channels file.
- **Source:** https://aca-prod.accela.com/ccsf/Cap/CapHome.aspx?module=Planning&TabName=Planning (checked 2026-11-26, live)

## Jail custody

### San Francisco Sheriff's jail custody lookup
- **Holds:** a lookup of who is in San Francisco County Jail custody now. A match shows the booking date, booking number, SF number, total bond, next court date, and court department. A person no longer in custody does not appear.
- **Access:** search by hand for a named person, by first and last name (at least two letters each) or by an exact SF number or booking number. It has no list or browse mode and no documented data feed. Use it only for a person the reporter already names in a public matter, such as someone named in a charging document or a public statement.
- **Cost:** free, no account.
- **Steps:** open https://sfsheriff.com/find-person-jail. Use this address without www: the www address redirects to the homepage.
- **The tools reach:** the DataSF dataset "Sheriff Jail Bookings", which counts bookings by month and demographic group. It holds monthly bookings, not a jail population count, and no person-level fields. The daily jail population appears only as a figure on the Sheriff's Office homepage, and no archive of it as data was found.
- **Ask:** the Sheriff's Office press line or records portal, in the request channels file. Questions worth sending: the jail population on a past date, and whether daily population figures are kept as data.
- **Source:** https://sfsheriff.com/find-person-jail (checked 2026-11-26, live)
- **Source:** https://sfsheriff.com/ (checked 2026-11-25, page)

## School board meetings

### San Francisco Board of Education (BoardDocs)
- **Holds:** San Francisco Unified School District Board of Education agendas, attached documents such as resolutions, budget presentations, school plans, labor contracts, and reports, and the board's minutes.
- **Access:** read in a browser; BoardDocs refuses programs.
- **Cost:** free, no login.
- **Steps:** open the district's calendar page, https://www.sfusd.edu/about-sfusd/board-education/board-meeting-calendar-and-agendas, which lists each meeting with a short agenda and a link to the full agenda on BoardDocs. Pick the meeting, then the agenda item; attached documents open from the item.
- **The tools reach:** nothing. Video of meetings with caption transcripts is on SFGovTV, at https://sanfrancisco.granicus.com/ViewPublisher.php?view_id=47. For what was said, see the transcript services below.
- **Ask:** the Board of Education Office, boardoffice@sfusd.edu or (415) 241-6427. Records requests go to the district's records mailbox, in the request channels file.
- **Source:** https://www.sfusd.edu/about-sfusd/board-education/board-meeting-calendar-and-agendas (checked 2026-11-26, page)
- **Source:** https://go.boarddocs.com/ca/sfusd/Board.nsf/Public (checked 2026-11-26, live)
- **Source:** https://sanfrancisco.granicus.com/ViewPublisher.php?view_id=47 (checked 2026-11-26, live)

## Public meeting transcripts

OpenCivicQuery reads Legistar meeting records, such as agenda items, actions, and votes, but not what was said. These services hold meeting transcripts.

### Hamlet
- **Holds:** meeting transcripts tied to speakers and video; it describes coverage of more than 1,700 cities and counties.
- **Access:** an account. Its homepage offers a paid service for teams and a separate free data access page.
- **Cost:** not published.
- **Source:** https://www.myhamlet.com/ (checked 2026-11-20, page)

### LocalLens
- **Holds:** annotated meeting transcripts and summaries; its county filters cover 19 states. American Public Media owns it.
- **Access:** an account.
- **Cost:** Starter $20, Professional $38, Advanced $59 a month; Enterprise by quote.
- **Source:** https://locallens.io/ (checked 2026-11-20, page)

### GatherGov
- **Holds:** meeting transcripts, agendas, and minutes; it describes coverage of more than 10,000 jurisdictions and offers a transcript API.
- **Access:** an account; it says to start for free.
- **Cost:** not published.
- **Source:** https://gathergov.com/api (checked 2026-11-20, page)

### citymeetings.nyc
- **Holds:** New York City Council and Charter Revision Commission meetings, divided into segments with AI and human review.
- **Access:** its website.
- **Cost:** not stated.
- **Source:** https://citymeetings.nyc/ (checked 2026-11-20, page)

### CivicSearch
- **Holds:** searchable transcript snippets, with timestamps, of meetings that 613 places post on YouTube.
- **Access:** search by hand on its website. It publishes no API.
- **Cost:** free.
- **Source:** https://civicsearch.org/ (checked 2026-11-20, live)

### Documenters
- **Holds:** notes on public meetings.
- **Access:** its website. Its terms forbid automated extraction without permission.
- **Cost:** free.
- **Source:** https://www.documenters.org/terms-of-service/ (checked 2026-11-20, page)

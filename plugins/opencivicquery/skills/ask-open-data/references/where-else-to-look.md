# Where else to look

The OpenCivicQuery and CourtListener tools reach free, public records that a machine may read. Many records a lead depends on sit elsewhere: behind a subscription, behind a per-page fee, or on a public website that forbids automated access. This file lists those channels so a reporter knows where a record may be and how to get it, and can tell a source which channel to check.

Every entry comes from the pages named in its Source lines, checked on the date shown. Prices change. Coverage claims are the vendor's own.

## How to use this file

- **Use it after the tools come up short,** or when the reporter asks where else a record might be. Free records come first: run the tools before you point anywhere else.
- **Say that OpenCivicQuery did not search the channel.** Never say a channel holds a specific record. Say what kind of records it describes itself as holding.
- **For each channel, give** what it holds, how a reporter gets access with every limit the entry lists, the published price, the date the entry was checked, and the link. When a price comes from a dated document, give that document's date too. When the price is not published, say so.
- **List every entry that fits the record type.** Entry order is not a ranking, and a Claude connector is not a recommendation. Do not recommend one vendor over another.
- **Where an entry says a site allows search by hand,** say so together with that entry's access limits. Never suggest a way around a site's terms or blocks.

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

## Immigration court

### TRAC Immigration
- **Holds:** immigration court analyses and data tools.
- **Access:** a subscription. Its terms bar exposing TRAC data through an AI system and require crediting TRAC with its full web address when reproducing a table or graph. A reporter may read it directly.
- **Cost:** $470 one-time setup, then yearly fees for news organizations by daily circulation: up to 150,000 is $1,465; 150,001 to 300,000 is $2,225; 300,001 to 600,000 is $3,120; 600,001 to 900,000 is $4,370.
- **Source:** https://tracreports.org/notices/fee_site.html (checked 2026-11-20, page)
- **Source:** https://tracreports.org/terms/ (checked 2026-11-20, page)

### EOIR case status (ACIS)
- **Holds:** the status of one immigration court case.
- **Access:** lookup by hand with the person's A-number and nationality; the site uses a CAPTCHA.
- **Cost:** free.
- **Source:** https://acis.eoir.justice.gov/en/ (checked 2026-11-20, live)

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

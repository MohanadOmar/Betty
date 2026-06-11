"""
Betty V2 — EMC Strategy Group Intelligence Agent
=================================================
Architecture (per V2 functionality spec):

  DAILY SWEEP   (runs every day)
    Jina scrapes the news/gov URLs → Perplexity synthesizes a brief
    against the Daily prompt (Property Search + Websites Scraping +
    Agenda Monitoring + Legislative + Campaign + News/Media).

  MONDAY SWEEP  (Monday only, in addition to Daily)
    Per-URL Perplexity calls — one focused call per official agenda URL.
    Plus per-district Perplexity calls — one per Congressional race
    (TX-15, TX-21, TX-23, TX-28, TX-34).
    No Jina scraping; once-a-week sweep, Perplexity-only for accuracy.

  FRIDAY SWEEP  (Friday only, in addition to Daily)
    Same per-URL pattern as Monday — one Perplexity call per agenda URL,
    emphasizing late or revised postings since Monday.
    Per-district calls included to support the CAMPAIGN INTELLIGENCE
    section of the weekly wrap output.

Tools: Python • Jina AI (Daily only) • Perplexity sonar-pro • Base44 • Railway
"""

import os
import time
import threading
import schedule
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from http.server import HTTPServer, BaseHTTPRequestHandler

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
PERPLEXITY_KEY = os.environ.get("PERPLEXITY_KEY")
BASE44_API_KEY = os.environ.get("BASE44_API_KEY")
DASHBOARD_URL = "https://betty-emc-insight.base44.app/api/entities/IntelligenceBrief"

# Canonical V2 monitoring scope — 8 jurisdictions paired with their official
# agenda URLs. Single source of truth.
JURISDICTION_AGENDAS = [
    ("Pleasanton, Texas",                                       "https://pleasantontx.granicus.com/ViewPublisher.php?view_id=1"),
    ("Pearsall, Texas",                                         "https://www.cityofpearsall.org/government/city_council.php"),
    ("Poteet, Texas",                                           "https://www.poteettx.org/AgendaCenter/City-Council-1"),
    ("Pecos, Texas",                                            "https://www.pecostx.gov/129"),
    ("PEDC (Pecos Economic Development Corporation), Texas",    "https://www.pecosedc.com"),
    ("Reeves County, Texas",                                    "https://www.reevescounty.org/departments/commissioners"),
    ("Somerset, Texas",                                         "https://www.somersettx.gov/agendas"),
    ("Uvalde, Texas",                                           "https://uvaldetx.civicweb.net/Portal/MeetingTypeList.aspx"),
]

JURISDICTIONS = [j for j, _ in JURISDICTION_AGENDAS]
AGENDA_URLS = [u for _, u in JURISDICTION_AGENDAS]

# Congressional races monitored — used by Monday and Friday for per-district
# news calls.
CONGRESSIONAL_DISTRICTS = ["TX-15", "TX-21", "TX-23", "TX-28", "TX-34"]

# News and gov sources for the Daily Sweep — Jina scrapes these and feeds
# the content into the Perplexity synthesis call.
NEWS_AND_GOV_URLS = [
    "https://www.uvaldeleadernews.com",
    "https://www.pleasantonexpress.com",
    "https://www.wilsoncountynews.com",
    "https://www.uvaldetx.gov",
    "https://www.uvaldecounty.gov",
    "https://www.pleasantontx.gov",
    "https://www.atascosacounty.texas.gov",
    "https://www.floresvilletx.gov",
    "https://www.co.wilson.tx.us",
    "https://www.co.wilson.tx.us/page/wilson.Bids_RFPs",
    "https://www.ethics.state.tx.us",
]

# ─────────────────────────────────────────
# PROMPT BUILDING BLOCKS (V2)
# ─────────────────────────────────────────
CRITICAL_INSTRUCTIONS = """CRITICAL INSTRUCTIONS:
- Texas only. Exclude any results from other states.
- Use official sources only: city/county websites, Agenda Center, CivicClerk,
  Granicus, CivicWeb, or official PDFs. No blogs, forums, or aggregators.
- Prioritize the most recent postings, including newly posted or updated content.
- Actively check for same-day meetings even if posted within the last 24 hours.
- Never fabricate. If no data, say so explicitly.
- Never use # or * formatting. Plain text only.
"""

EMC_PRINCIPALS = """EMC PRINCIPALS:
- EMC Strategy Group
- Ernie Gonzalez Jr
- Janice Gonzalez
"""

INDIRECT_SIGNALS = """INDIRECT SIGNALS to flag even when EMC is not named:
- "contract amendment"
- "professional services agreement"
- "grant services"
- "consulting services"
- Any city or county discussion involving third-party consultants
- Contract awards, RFP/RFQ postings, or grant agreements in monitored jurisdictions
- Texas Ethics Commission lobbying disclosures
"""

# ─────────────────────────────────────────
# PROMPTS — per call type
# ─────────────────────────────────────────

# Used by per-URL agenda calls (Monday and Friday)
JURISDICTION_AGENDA_SYS_PROMPT = f"""You are the EMC Intelligence Agent reading one
official agenda page for a Texas jurisdiction on behalf of EMC Strategy Group.

{EMC_PRINCIPALS}

{CRITICAL_INSTRUCTIONS}

Read the URL the user provides. Find meetings scheduled for the current week
(Monday through Friday) including any same-day meetings within the next 48 hours.
For Friday-context calls, also flag late-posted or revised agendas since Monday.

Scan agenda text for any mention of EMC Strategy Group, Ernie Gonzalez Jr, or
Janice Gonzalez. If found, quote the exact language.

{INDIRECT_SIGNALS}

OUTPUT FORMAT (exactly this template, plain text):

JURISDICTION NAME, Texas
- Meeting:
- Date/Time:
- Agenda Link:
- Key Items: (2-3 max, focus on infrastructure / water / utilities / funding /
  grants / legislative coordination)
- EMC Mention: None detected
  OR
  EMC MENTION IDENTIFIED — "[exact quoted language]"

If no meeting found for this jurisdiction this week, write:
JURISDICTION NAME, Texas
- No meeting agenda found for this week.
"""

# Used by per-district news calls (Monday and Friday)
DISTRICT_NEWS_SYS_PROMPT = f"""You are the EMC Intelligence Agent gathering campaign
and political intelligence for EMC Strategy Group on Texas Congressional races.

{CRITICAL_INSTRUCTIONS}

Focus on candidate developments, withdrawals, endorsements, primary results, and
material changes. Recent only — last 7 days. Use credible news outlets and
official campaign sources.

OUTPUT FORMAT (plain text, exactly this template):

[DISTRICT]
- [One paragraph summary of material developments in the last 7 days, or:]
- Nothing material to report.
"""

# Used by the Daily synthesis call after Jina scraping
DAILY_SYS_PROMPT = f"""You are the EMC Intelligence Agent running the Daily EMC
Monitoring Sweep for EMC Strategy Group.

{EMC_PRINCIPALS}

MONITORED JURISDICTIONS:
{chr(10).join(f"  - {j}" for j in JURISDICTIONS)}

{CRITICAL_INSTRUCTIONS}

TIMEFRAME: Last 24-48 hours only.

You will receive scraped content from EMC's monitored news and government
sources. Analyze it AND run a live Perplexity search for any additional recent
developments. Cover all six areas:

1. PROPERTY SEARCH — Direct mentions of EMC Strategy Group, Ernie Gonzalez Jr,
   or Janice Gonzalez.
2. WEBSITES SCRAPING — Local/regional news, city council agendas, government
   sites, contract awards/amendments/grant agreements/RFPs, Texas Ethics
   Commission lobbying disclosures.
3. AGENDA MONITORING — References to EMC, consulting services, contracts, RFPs.
4. LEGISLATIVE AND POLICY — Texas Legislature and U.S. Congress activity.
5. CAMPAIGN AND POLITICAL — Brief mention if anything urgent broke today.
6. NEWS AND MEDIA — Credible local/regional news affecting EMC clients.

{INDIRECT_SIGNALS}

OUTPUT FORMAT (exactly this structure):

SUMMARY
[One line: "No new developments in the last 48 hours" OR a one-line headline
if something is found.]

FINDINGS
For each finding:
  Source | Date
  1-2 sentence summary
  Why it matters: contract / funding / government relations impact

If no findings: "No material findings in the last 48 hours."

EARLY SIGNALS
[Any agenda items, upcoming meetings, or indirect indicators worth watching.
If none: "No early signals at this time."]

STATUS
Ongoing monitoring continues. Next daily sweep tomorrow 7:30 AM CT.
"""

# Used to wrap the per-URL and per-district results into a Monday brief
MONDAY_WRAP_HEADER = """MONDAY MORNING AGENDA SWEEP
{date}

AGENDAS — Monitored Jurisdictions (current week, Monday-Friday)
================================================================
{agendas}

CAMPAIGN INTELLIGENCE — Texas Congressional Races
================================================================
{districts}

STATUS
================================================================
Monday agenda sweep complete. Next sweep: Daily tomorrow at 7:30 AM CT;
Friday weekly wrap at end of week.
"""

# Used to wrap the per-URL and per-district results into a Friday brief
FRIDAY_WRAP_HEADER = """FRIDAY AFTERNOON WEEKLY WRAP
{date}

LATE AND REVISED AGENDAS — Monitored Jurisdictions
================================================================
{agendas}

CAMPAIGN INTELLIGENCE — Texas Congressional Races
================================================================
{districts}

STATUS
================================================================
Friday weekly wrap complete. Next sweep: Monday at 7:30 AM CT.
"""

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


def clean_html(html):
    """Strip HTML to readable text — used by Daily Sweep's Jina scrape."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "meta", "link"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def fetch_url(url, char_limit=4000):
    """Fetch a URL via Jina Reader, clean it, and truncate. Daily Sweep only."""
    try:
        r = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"X-Return-Format": "markdown"},
            timeout=30,
        )
        if r.status_code == 200:
            content = clean_html(r.text)[:char_limit]
            log(f"  Jina OK — {len(content)} chars from {url}")
            return content
        log(f"  Jina failed: {r.status_code} for {url}")
        return f"STATUS: Failed {r.status_code}"
    except Exception as e:
        log(f"  Jina error: {e} for {url}")
        return f"STATUS: Error - {str(e)}"


def call_perplexity(sys_prompt, user_content, recency_filter="day"):
    """Single Perplexity sonar-pro call."""
    response = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={
            "Authorization": f"Bearer {PERPLEXITY_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "sonar-pro",
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_content},
            ],
            "search_recency_filter": recency_filter,
        },
        timeout=60,
    )
    return response.json()["choices"][0]["message"]["content"]


def classify_priority(content):
    """Tier briefs P1 / P2 / P3 for the dashboard."""
    lower = content.lower()
    p1_markers = [
        "emc strategy group", "ernie gonzalez", "janice gonzalez",
        "emc mention identified",
    ]
    p2_markers = [
        "contract amendment", "professional services agreement",
        "grant services", "third-party consultant", "third party consultant",
        "consulting services", "lobbying disclosure",
    ]
    if any(m in lower for m in p1_markers):
        return "P1"
    if any(m in lower for m in p2_markers):
        return "P2"
    return "P3"


# ─────────────────────────────────────────
# PER-URL AND PER-DISTRICT CALLS (Monday and Friday)
# ─────────────────────────────────────────
def fetch_jurisdiction_agenda(jurisdiction, url, friday_context=False):
    """One Perplexity call for one jurisdiction's official agenda URL."""
    context_note = ""
    if friday_context:
        context_note = (
            "This is a Friday wrap call — emphasize agendas posted in the last "
            "48-72 hours, late-posted or revised agendas since Monday's sweep, "
            "and any meetings that may have been missed earlier in the week.\n\n"
        )

    user_prompt = (
        f"{context_note}"
        f"Jurisdiction: {jurisdiction}\n"
        f"Read this official agenda page directly and find the upcoming "
        f"agendas: {url}\n\n"
        f"Return your output in the per-entity template from the system prompt, "
        f"using \"{jurisdiction}\" as the jurisdiction header."
    )
    return call_perplexity(
        JURISDICTION_AGENDA_SYS_PROMPT,
        user_prompt,
        recency_filter="week",
    )


def fetch_district_news(district):
    """One Perplexity call for one Congressional district."""
    user_prompt = (
        f"What are the latest news in {district}? Focus on candidate "
        f"developments, withdrawals, endorsements, and any material changes "
        f"from the last 7 days. Use the output template from your system "
        f"prompt with \"{district}\" as the header."
    )
    return call_perplexity(
        DISTRICT_NEWS_SYS_PROMPT,
        user_prompt,
        recency_filter="week",
    )


# ─────────────────────────────────────────
# MONDAY AGENDA SWEEP
# ─────────────────────────────────────────
def monday_agenda_sweep():
    log("Starting Monday Morning Agenda Sweep (V2)...")

    agenda_blocks = []
    for jurisdiction, url in JURISDICTION_AGENDAS:
        log(f"  Perplexity call: {jurisdiction}")
        try:
            block = fetch_jurisdiction_agenda(jurisdiction, url, friday_context=False)
            agenda_blocks.append(block)
        except Exception as e:
            log(f"  ERROR fetching {jurisdiction}: {e}")
            agenda_blocks.append(
                f"{jurisdiction}\n- Error: could not retrieve agenda this run."
            )

    district_blocks = []
    for district in CONGRESSIONAL_DISTRICTS:
        log(f"  Perplexity call: {district}")
        try:
            block = fetch_district_news(district)
            district_blocks.append(block)
        except Exception as e:
            log(f"  ERROR fetching {district}: {e}")
            district_blocks.append(f"{district}\n- Error: could not retrieve news this run.")

    final = MONDAY_WRAP_HEADER.format(
        date=datetime.now().strftime("%A, %B %d, %Y"),
        agendas="\n\n".join(agenda_blocks),
        districts="\n\n".join(district_blocks),
    )

    log("Monday Agenda Sweep complete.")
    print("\n" + "=" * 60)
    print(final)
    print("=" * 60 + "\n")

    send_to_dashboard("monday_agenda", final)


# ─────────────────────────────────────────
# FRIDAY SWEEP — WEEKLY WRAP
# ─────────────────────────────────────────
def friday_sweep():
    log("Starting Friday Afternoon Weekly Wrap (V2)...")

    agenda_blocks = []
    for jurisdiction, url in JURISDICTION_AGENDAS:
        log(f"  Perplexity call: {jurisdiction} (Friday context)")
        try:
            block = fetch_jurisdiction_agenda(jurisdiction, url, friday_context=True)
            agenda_blocks.append(block)
        except Exception as e:
            log(f"  ERROR fetching {jurisdiction}: {e}")
            agenda_blocks.append(
                f"{jurisdiction}\n- Error: could not retrieve agenda this run."
            )

    district_blocks = []
    for district in CONGRESSIONAL_DISTRICTS:
        log(f"  Perplexity call: {district}")
        try:
            block = fetch_district_news(district)
            district_blocks.append(block)
        except Exception as e:
            log(f"  ERROR fetching {district}: {e}")
            district_blocks.append(f"{district}\n- Error: could not retrieve news this run.")

    final = FRIDAY_WRAP_HEADER.format(
        date=datetime.now().strftime("%A, %B %d, %Y"),
        agendas="\n\n".join(agenda_blocks),
        districts="\n\n".join(district_blocks),
    )

    log("Friday Sweep complete.")
    print("\n" + "=" * 60)
    print(final)
    print("=" * 60 + "\n")

    send_to_dashboard("friday_weekly", final)


# ─────────────────────────────────────────
# DAILY EMC MONITORING SWEEP — Jina scrape + Perplexity synthesis
# ─────────────────────────────────────────
def daily_monitoring_sweep():
    log("Starting Daily EMC Monitoring Sweep (V2)...")

    # Step 1: Jina scrape the news/gov sources
    scraped = ""
    for url in NEWS_AND_GOV_URLS:
        log(f"  Scraping {url}...")
        content = fetch_url(url, char_limit=3000)
        scraped += f"\n\n{'='*50}\nSOURCE: {url}\n{'='*50}\n{content}\n"

    log(f"Total scraped: {len(scraped)} chars across {len(NEWS_AND_GOV_URLS)} sources")

    # Step 2: Hand the scraped content to Perplexity for synthesis +
    # live search of anything we didn't scrape
    user_query = f"""Run the V2 Daily Sweep now. Below is scraped content from
EMC's monitored news and government sources. Analyze it AND run a live web
search for any additional recent developments in the last 24-48 hours related
to EMC Strategy Group, Ernie Gonzalez Jr, or Janice Gonzalez and the monitored
jurisdictions.

Cover all six search areas listed in your system instructions. Report only
verified findings from the last 48 hours.

SCRAPED CONTENT:

{scraped}
"""

    result = call_perplexity(
        DAILY_SYS_PROMPT,
        user_query,
        recency_filter="day",
    )

    log("Daily EMC Monitoring Sweep complete.")
    print("\n" + "=" * 60)
    print("DAILY EMC MONITORING BRIEF (V2)")
    print("=" * 60)
    print(result)
    print("=" * 60 + "\n")

    send_to_dashboard("daily", result)


# ─────────────────────────────────────────
# COMBINED DAILY RUN — 7:30 AM CT
# ─────────────────────────────────────────
def daily_run():
    today = datetime.now().strftime("%A")
    log(f"Daily run triggered - {today}")

    if today == "Monday":
        monday_agenda_sweep()

    if today == "Friday":
        friday_sweep()

    # Daily EMC monitoring runs every day
    daily_monitoring_sweep()
    monday_agenda_sweep()
    friday_sweep()


# ─────────────────────────────────────────
# BASE44 DASHBOARD POST
# ─────────────────────────────────────────
def send_to_dashboard(sweep_type, content):
    """Push brief to Base44 with priority tier (P1/P2/P3)."""
    try:
        priority = classify_priority(content)
        has_priority = priority in ("P1", "P2")
        has_emc_mention = (
            "EMC Strategy Group" in content
            or "Ernie Gonzalez" in content
            or "Janice Gonzalez" in content
        )
        r = requests.post(
            DASHBOARD_URL,
            json={
                "sweep_type": sweep_type,
                "timestamp": datetime.now().isoformat(),
                "content": content,
                "has_priority": has_priority,
                "has_emc_mention": has_emc_mention,
                "priority_tier": priority,
                "version": "v2",
            },
            headers={
                "api_key": BASE44_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        log(f"Dashboard updated - status {r.status_code}, tier={priority}")
        if r.status_code not in (200, 201):
            log(f"Dashboard error - {r.text[:300]}")
    except Exception as e:
        log(f"Dashboard push failed - {e}")


# ─────────────────────────────────────────
# HEALTH CHECK SERVER (Railway requirement)
# ─────────────────────────────────────────
class HealthCheck(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"EMC Agent V2 - OK")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheck)
    log(f"Health check server running on port {port}")
    server.serve_forever()


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    log("EMC Intelligence Agent V2 starting...")

    threading.Thread(target=start_health_server, daemon=True).start()

    # Run once immediately on startup
    daily_run()

    # Schedule daily at 7:30 AM
    schedule.every().day.at("07:30").do(daily_run)

    log("Scheduler running - waiting for next trigger...")
    while True:
        schedule.run_pending()
        time.sleep(60)

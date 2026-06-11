"""
Betty V2 — EMC Strategy Group Intelligence Agent
=================================================
Updated from V1 to match the V2 architecture spec:

  * Three sweeps with explicit structure:
      - Daily Sweep   (Property Search + Websites Scraping + Agenda/Lege/Campaign/News)
      - Monday Sweep  (current-week agendas, per-entity output)
      - Friday Sweep  (weekly wrap, late/revised agendas, lege, campaigns, grants)
  * 8 canonical jurisdictions locked in (V2 monitoring scope)
  * INDIRECT signals broken out as a named block
  * Per-entity output template enforced for agenda sweeps
  * Critical Instructions header prepended to every prompt
  * Priority tiering (P1/P2/P3) on dashboard payload

Tools: Python • Jina AI • Perplexity API (sonar-pro) • Base44 • Railway
"""

import os
import re
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

# Canonical V2 monitoring scope — 8 jurisdictions.
# This is the source of truth. Prompts reference this list explicitly.
JURISDICTIONS = [
    "Pleasanton, Texas",
    "Pearsall, Texas",
    "Poteet, Texas",
    "Pecos, Texas",
    "PEDC (Pecos Economic Development Corporation), Texas",
    "Reeves County, Texas",
    "Somerset, Texas",
    "Uvalde, Texas",
]

# Agenda URLs — Monday and Friday sweeps scrape these.
# One URL per V2 jurisdiction where available.
AGENDA_URLS = [
    "https://pleasantontx.granicus.com/ViewPublisher.php?view_id=1",      # Pleasanton
    "https://www.cityofpearsall.org/government/city_council.php",         # Pearsall (V2 new)
    "https://www.poteettx.org/AgendaCenter/City-Council-1",               # Poteet
    "https://www.pecostx.gov/129",                                        # Pecos
    "https://www.pecosedc.com",                                           # PEDC (V2 new)
    "https://www.reevescounty.org/departments/commissioners",             # Reeves County (V2 new)
    "https://www.somersettx.gov/agendas",                                 # Somerset
    "https://uvaldetx.civicweb.net/Portal/MeetingTypeList.aspx",          # Uvalde
    # Removed in V2 (out of monitoring scope):
    # "https://www.centertexas.org/city-council/agendas-minutes",        # Center, TX (East TX, not a V2 jurisdiction)
    # "https://www.co.wilson.tx.us/page/wilson.ccagendas",               # Wilson County (kept in NEWS_AND_GOV_URLS for indirect signals)
]

# Supplementary news / gov sites — referenced by the Daily Sweep prompt
# for indirect signal detection (contract awards, RFPs, ethics filings, etc.)
# These are NOT scraped directly; Perplexity uses them as a domain allowlist hint.
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

PER_ENTITY_FORMAT = """PER-ENTITY OUTPUT FORMAT (use exactly this template for each jurisdiction):

JURISDICTION NAME, Texas
- Meeting:
- Date/Time:
- Agenda Link:
- Key Items: (2-3 max, focus on infrastructure / water / utilities / funding / grants / legislative coordination)
- EMC Mention: None detected
  OR
  EMC MENTION IDENTIFIED — "[exact quoted language]"

If no meeting found for a jurisdiction this week, write:
JURISDICTION NAME, Texas
- No meeting agenda found for this week.
"""

JURISDICTION_BLOCK = "MONITORED JURISDICTIONS (cover all 8 in your output):\n" + \
    "\n".join(f"  {i+1}. {j}" for i, j in enumerate(JURISDICTIONS))

# ─────────────────────────────────────────
# PROMPTS (V2)
# ─────────────────────────────────────────
AGENDA_SYS_PROMPT = f"""You are the EMC Intelligence Agent running the Monday Morning Agenda Sweep
for EMC Strategy Group.

{EMC_PRINCIPALS}
{JURISDICTION_BLOCK}

{CRITICAL_INSTRUCTIONS}

TIMEFRAME: Current week (Monday through Friday), including any meetings
happening today or within the next 48 hours.

FOR EACH JURISDICTION, extract:
  - Meeting name
  - Date and time
  - Direct agenda link (PDF or webpage)
  - Key items (2-3 max), focused on infrastructure, water/utilities,
    funding/grants, or legislative/intergovernmental coordination
  - EMC mention scan (use exact quoted language if found)

{INDIRECT_SIGNALS}

{PER_ENTITY_FORMAT}

End your response with this status line:
STATUS: Monday agenda sweep complete. Cover all 8 jurisdictions above.
"""

DAILY_SYS_PROMPT = f"""You are the EMC Intelligence Agent running the Daily EMC Monitoring Sweep
for EMC Strategy Group.

{EMC_PRINCIPALS}
{JURISDICTION_BLOCK}

{CRITICAL_INSTRUCTIONS}

TIMEFRAME: Last 24-48 hours only.

SEARCH SCOPE — cover all six of these areas in one synthesized brief:

1. PROPERTY SEARCH — Direct mentions of EMC Strategy Group, Ernie Gonzalez Jr,
   or Janice Gonzalez anywhere on the public web.

2. WEBSITES SCRAPING — Scan local and regional news outlets (including small
   sources), city council agendas/minutes/PDFs, government and municipal
   websites, contract awards/amendments/grant agreements/RFPs, and Texas Ethics
   Commission lobbying disclosures if relevant.

3. AGENDA MONITORING (Municipal and County) — Review upcoming agendas for the
   monitored cities and counties. Identify references to EMC Strategy Group,
   consulting services, contracts, or RFPs. Track meeting dates and times.

4. LEGISLATIVE AND POLICY MONITORING — Texas Legislature and U.S. Congress.
   Bills, hearings, committee activity, funding opportunities tied to client
   priorities.

5. CAMPAIGN AND POLITICAL INTELLIGENCE — Congressional races TX-21, TX-23,
   TX-15, TX-28, TX-34. Candidate developments, withdrawals, endorsements,
   major updates.

6. NEWS AND MEDIA MONITORING — Credible local and regional news sources,
   policy updates, political developments affecting EMC clients.

{INDIRECT_SIGNALS}

OUTPUT FORMAT (use exactly this structure):

SUMMARY
[One line: "No new developments in the last 48 hours" OR a one-line headline if
something is found.]

FINDINGS
For each finding:
  Source | Date
  1-2 sentence summary
  Why it matters: contract / funding / government relations impact

If no findings: "No material findings in the last 48 hours."

EARLY SIGNALS
Any agenda items, upcoming meetings, or indirect indicators worth watching.
If none: "No early signals at this time."

STATUS
Ongoing monitoring continues. Next daily sweep tomorrow 7:30 AM CT.
"""

FRIDAY_SYS_PROMPT = f"""You are the EMC Intelligence Agent running the Friday Afternoon Weekly Wrap
for EMC Strategy Group.

{EMC_PRINCIPALS}
{JURISDICTION_BLOCK}

{CRITICAL_INSTRUCTIONS}

TIMEFRAME: Current week (Monday through Friday), with emphasis on:
  - Agendas posted in the last 48-72 hours
  - Late-posted or revised agendas since Monday's sweep
  - Meetings that may have been missed earlier in the week

Deliver a consolidated weekly intelligence summary. Every section must appear
even if empty.

OUTPUT FORMAT:

PRIORITY — EMC MENTIONS
[Finding with exact quoted language OR "Nothing to report."]

LATE AND REVISED AGENDAS
[Per-entity findings for any agendas posted/updated since Monday OR
"Nothing new since Monday sweep."]

LEGISLATIVE UPDATE
[Texas Legislature and US Congress activity this week OR "Nothing to report."]

CAMPAIGN INTELLIGENCE
TX-21 · [development or "Nothing to report."]
TX-23 · [development or "Nothing to report."]
TX-15 · [development or "Nothing to report."]
TX-28 · [development or "Nothing to report."]
TX-34 · [development or "Nothing to report."]

GRANTS AND OPPORTUNITIES
[New grant announcements and RFP/RFQ postings from the last 7 days OR
"Nothing to report."]

{INDIRECT_SIGNALS}

WEEKLY SUMMARY
- Jurisdictions checked: 8
- EMC mentions: [count or "None"]
- Grant deadlines requiring action: [list or "None"]

STATUS
Friday weekly wrap complete. Next sweep: Monday at 7:30 AM CT.
"""

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def clean_html(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "meta", "link"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def fetch_url(url, char_limit=8000):
    """Fetch a URL via Jina Reader, clean it, and truncate."""
    try:
        r = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"X-Return-Format": "markdown"},
            timeout=30,
        )
        if r.status_code == 200:
            content = clean_html(r.text)[:char_limit]
            print(f"  OK - {len(content)} chars")
            return content
        else:
            print(f"  Failed: {r.status_code}")
            return f"STATUS: Failed {r.status_code}"
    except Exception as e:
        print(f"  Error: {e}")
        return f"STATUS: Error - {str(e)}"


def call_perplexity(sys_prompt, user_content, recency_filter="day"):
    """Call Perplexity sonar-pro with system + user messages."""
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


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


def classify_priority(content):
    """
    V2 enhancement: tier findings P1 / P2 / P3.
      P1 — Direct EMC / Ernie / Janice mention
      P2 — Indirect signal (third-party consultant, prof services agreement, etc.)
      P3 — Context only (lege, campaign, grant) — default
    """
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
# MONDAY AGENDA SWEEP
# ─────────────────────────────────────────
def monday_agenda_sweep():
    log("Starting Monday Morning Agenda Sweep (V2)...")
    scraped = ""
    agenda_links = []

    for url in AGENDA_URLS:
        log(f"Fetching {url}...")
        content = fetch_url(url, char_limit=8000)
        scraped += f"\n\n{'='*50}\nSOURCE: {url}\n{'='*50}\n{content}\n"

        # Extract AgendaViewer / agenda detail links for follow-up fetching
        for line in content.split("\n"):
            if "AgendaViewer" in line or "agenda" in line.lower():
                links = re.findall(r"https?://[^\s\)\"]+", line)
                for link in links:
                    if "AgendaViewer" in link and link not in agenda_links:
                        agenda_links.append(link)

    log(f"Found {len(agenda_links)} agenda viewer links - fetching up to 5...")
    for link in agenda_links[:5]:
        log(f"  Fetching agenda detail: {link}")
        detail = fetch_url(link, char_limit=5000)
        scraped += f"\n\n{'='*50}\nAGENDA DETAIL: {link}\n{'='*50}\n{detail}\n"

    log(f"Total scraped content: {len(scraped)} chars")
    log("Sending to Perplexity for analysis...")

    result = call_perplexity(
        AGENDA_SYS_PROMPT,
        f"Extract all meeting agendas from this scraped content. "
        f"Cover all 8 monitored jurisdictions:\n\n{scraped}",
        recency_filter="week",
    )

    log("Monday Agenda Sweep complete.")
    print("\n" + "=" * 60)
    print("MONDAY MORNING AGENDA SWEEP (V2)")
    print("=" * 60)
    print(result)
    print("=" * 60 + "\n")

    send_to_dashboard("monday_agenda", result)


# ─────────────────────────────────────────
# DAILY EMC MONITORING SWEEP
# ─────────────────────────────────────────
def daily_monitoring_sweep():
    log("Starting Daily EMC Monitoring Sweep (V2)...")

    # Build the explicit source domain hint from NEWS_AND_GOV_URLS
    source_hint = "\n".join(f"  - {u}" for u in NEWS_AND_GOV_URLS)

    user_query = f"""Run the V2 Daily Sweep now. Search the web for any direct or indirect
mentions related to EMC Strategy Group, Ernie Gonzalez Jr, and Janice Gonzalez
across Texas news, government records, contract awards, meeting minutes, and
public announcements in the last 24-48 hours.

Monitored jurisdictions:
{chr(10).join(f"  - {j}" for j in JURISDICTIONS)}

Also scan these supplementary news/gov sources for indirect signals:
{source_hint}

Cover all six search areas listed in your instructions (Property Search,
Websites Scraping, Agenda Monitoring, Legislative/Policy, Campaign/Political,
News/Media). Report only verified findings from the last 48 hours.
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
# FRIDAY SWEEP — WEEKLY WRAP
# ─────────────────────────────────────────
def friday_sweep():
    log("Starting Friday Afternoon Weekly Wrap (V2)...")
    scraped = ""

    # Re-scrape agendas to catch late/revised postings since Monday
    for url in AGENDA_URLS:
        log(f"Fetching {url}...")
        content = fetch_url(url, char_limit=5000)
        scraped += f"\n\n{'='*50}\nSOURCE: {url}\n{'='*50}\n{content}\n"

    log(f"Agenda scrape complete - {len(scraped)} chars")
    log("Sending to Perplexity for Friday weekly wrap...")

    user_query = f"""Run the V2 Friday Weekly Wrap. Analyze this week's scraped agenda
content with emphasis on late-posted or revised agendas since Monday.
Also pull in Texas Legislature, US Congress, TX-21/23/15/28/34 campaign
intelligence, and any new grant or RFP announcements from the last 7 days.

Monitored jurisdictions:
{chr(10).join(f"  - {j}" for j in JURISDICTIONS)}

Scraped agenda content:

{scraped}
"""

    result = call_perplexity(
        FRIDAY_SYS_PROMPT,
        user_query,
        recency_filter="week",
    )

    log("Friday Sweep complete.")
    print("\n" + "=" * 60)
    print("FRIDAY WEEKLY WRAP (V2)")
    print("=" * 60)
    print(result)
    print("=" * 60 + "\n")

    send_to_dashboard("friday_weekly", result)


# ─────────────────────────────────────────
# COMBINED DAILY RUN — 7:30 AM CT every day
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
    """V2: pushes brief to Base44 with priority tier (P1/P2/P3)."""
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
                "priority_tier": priority,          # V2 new
                "version": "v2",                    # V2 new
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
        pass  # suppress access logs


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

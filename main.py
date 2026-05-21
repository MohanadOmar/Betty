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

AGENDA_URLS = [
    "https://pleasantontx.granicus.com/ViewPublisher.php?view_id=1",
    "https://www.poteettx.org/AgendaCenter/City-Council-1",
    "https://www.centertexas.org/city-council/agendas-minutes",
    "https://www.pecostx.gov/129",
    "https://www.pecostx.gov/Calendar.aspx",
    "https://www.somersettx.gov/agendas",
    "https://www.uvaldetx.gov/government/city_council/agendas___minutes.php",
    "https://www.co.wilson.tx.us/page/wilson.ccagendas",
]

NEWS_AND_GOV_URLS = [
    "https://www.uvaldeleadernews.com",
    "https://www.pleasantonexpress.com",
    "https://www.wilsoncountynews.com",
    "https://www.uvaldetx.gov",
    "https://www.uvaldecounty.gov",
    "https://www.pleasantontx.gov",
    "https://www.atascosacounty.texas.gov",
    "https://www.floresvilletx.gov",
    "https://www.floresvilletx.gov/government/agendas-minutes/",
    "https://www.co.wilson.tx.us",
    "https://www.co.wilson.tx.us/page/wilson.Bids_RFPs",
    "https://www.co.wilson.tx.us/page/wilson.ccagendas",
    "https://www.ethics.state.tx.us",
]

# ─────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────
AGENDA_SYS_PROMPT = """
You are the EMC Intelligence Agent analyzing scraped Texas city council agenda 
pages for EMC Strategy Group. Your principals are Ernie Gonzalez Jr and Janice Gonzalez.

Extract every meeting found this week. For each jurisdiction return exactly:
- Meeting name
- Date and time
- Direct agenda link
- Key items (infrastructure, water, grants, contracts, consulting, RFPs - 3 max)
- EMC mention: scan for EMC Strategy Group, Ernie Gonzalez Jr, Janice Gonzalez

If a jurisdiction has no agenda this week write: No agenda found this week.
If agenda exists but items unclear write: Agenda posted - items require manual review. Include the link.
Never fabricate. Never use # or *. Cover all 8 jurisdictions.

Format each jurisdiction as:
JURISDICTION NAME, Texas
- Meeting:
- Date/Time:
- Agenda link:
- Key items:
- EMC mention: None detected / EMC MENTION IDENTIFIED - [exact quote]
"""

DAILY_SYS_PROMPT = """
You are the EMC Intelligence Agent. Analyze the scraped content provided and 
identify any mentions of EMC Strategy Group, Ernie Gonzalez Jr, and Janice Gonzalez.

Also scan for indirect signals:
- "contract amendment"
- "professional services agreement"
- "grant services"
- Any city discussions involving third-party consultants even if EMC is not named

Texas only. Last 24-48 hours only. Never fabricate. Never use # or *.

Output format:

SUMMARY
One line: No new developments in the last 48 hours
OR one line headline if something is found.

FINDINGS
Source | Date
1-2 sentence summary.
Why it matters: contract / funding / government relations impact.

EARLY SIGNALS
Any agenda items, upcoming meetings, or indirect indicators worth watching.

STATUS
Ongoing monitoring continues.
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


# ─────────────────────────────────────────
# MONDAY AGENDA SWEEP
# ─────────────────────────────────────────
def monday_agenda_sweep():
    log("Starting Monday Morning Agenda Sweep...")
    scraped = ""
    agenda_links = []

    for url in AGENDA_URLS:
        log(f"Fetching {url}...")
        content = fetch_url(url, char_limit=8000)
        scraped += f"\n\n{'='*50}\nSOURCE: {url}\n{'='*50}\n{content}\n"

        # Extract AgendaViewer links
        for line in content.split("\n"):
            if "AgendaViewer" in line or "agenda" in line.lower():
                links = re.findall(r"https?://[^\s\)\"]+", line)
                for link in links:
                    if "AgendaViewer" in link and link not in agenda_links:
                        agenda_links.append(link)

    # Fetch up to 5 agenda detail pages
    log(f"Found {len(agenda_links)} agenda viewer links - fetching up to 5...")
    for link in agenda_links[:5]:
        log(f"  Fetching agenda detail: {link}")
        detail = fetch_url(link, char_limit=5000)
        scraped += f"\n\n{'='*50}\nAGENDA DETAIL: {link}\n{'='*50}\n{detail}\n"

    log(f"Total scraped content: {len(scraped)} chars")
    log("Sending to Perplexity for analysis...")

    result = call_perplexity(
        AGENDA_SYS_PROMPT,
        f"Extract all meeting agendas from this scraped content:\n\n{scraped}",
        recency_filter="week",
    )

    log("Monday Agenda Sweep complete.")
    print("\n" + "=" * 60)
    print("MONDAY MORNING AGENDA SWEEP")
    print("=" * 60)
    print(result)
    print("=" * 60 + "\n")


# ─────────────────────────────────────────
# DAILY EMC MONITORING SWEEP
# ─────────────────────────────────────────
def daily_monitoring_sweep():
    log("Starting Daily EMC Monitoring Sweep...")
    scraped = ""

    for url in NEWS_AND_GOV_URLS:
        log(f"Fetching {url}...")
        content = fetch_url(url, char_limit=5000)
        scraped += f"\n\n{'='*50}\nSOURCE: {url}\n{'='*50}\n{content}\n"

    log(f"Total scraped content: {len(scraped)} chars")
    log("Sending to Perplexity for EMC analysis...")

    result = call_perplexity(
        DAILY_SYS_PROMPT,
        f"Analyze this scraped content for EMC mentions and indirect signals:\n\n{scraped}",
        recency_filter="day",
    )

    log("Daily EMC Monitoring Sweep complete.")
    print("\n" + "=" * 60)
    print("DAILY EMC MONITORING BRIEF")
    print("=" * 60)
    print(result)
    print("=" * 60 + "\n")


# ─────────────────────────────────────────
# FRIDAY SWEEP - AGENDA + FULL INTELLIGENCE
# ─────────────────────────────────────────
FRIDAY_SYS_PROMPT = """
You are the EMC Intelligence Agent running the Friday Afternoon Sweep for 
EMC Strategy Group. Your principals are Ernie Gonzalez Jr and Janice Gonzalez.

Deliver a consolidated weekly intelligence summary. Cover:
1. Any late-posted or revised agendas since Monday
2. Texas Legislature and US Congress activity this week
3. TX-21, TX-23, TX-15, TX-28, TX-34 congressional race developments
4. New grant announcements and RFP/RFQ postings last 7 days
5. EMC Strategy Group, Ernie Gonzalez Jr, Janice Gonzalez - any news this week

Never fabricate. Never use # or *. Every section must appear even if empty.

Output format:

PRIORITY - EMC MENTIONS
[Finding or: Nothing to report.]

LATE AND REVISED AGENDAS
[Findings or: Nothing new since Monday sweep.]

LEGISLATIVE UPDATE
[Findings or: Nothing to report.]

CAMPAIGN INTELLIGENCE
TX-21 · [development or Nothing to report.]
TX-23 · [development or Nothing to report.]
TX-15 · [development or Nothing to report.]
TX-28 · [development or Nothing to report.]
TX-34 · [development or Nothing to report.]

GRANTS AND OPPORTUNITIES
[Findings or: Nothing to report.]

WEEKLY SUMMARY
- Jurisdictions checked: [X]
- EMC mentions: [X or None]
- Grant deadlines requiring action: [list or None]

STATUS
Friday sweep complete. Next sweep: Monday at 7:30 AM CT.
"""


def friday_sweep():
    log("Starting Friday Afternoon Sweep...")
    scraped = ""

    # Scrape agendas
    for url in AGENDA_URLS:
        log(f"Fetching {url}...")
        content = fetch_url(url, char_limit=5000)
        scraped += f"\n\n{'='*50}\nSOURCE: {url}\n{'='*50}\n{content}\n"

    log(f"Agenda scrape complete - {len(scraped)} chars")
    log("Sending to Perplexity for Friday weekly wrap...")

    result = call_perplexity(
        FRIDAY_SYS_PROMPT,
        f"Analyze this week's scraped agenda content and deliver the Friday weekly wrap:\n\n{scraped}",
        recency_filter="week",
    )

    log("Friday Sweep complete.")
    print("\n" + "=" * 60)
    print("FRIDAY WEEKLY WRAP")
    print("=" * 60)
    print(result)
    print("=" * 60 + "\n")


# ─────────────────────────────────────────
# COMBINED DAILY RUN - 7:30 AM EVERY DAY
# Runs agenda sweep on Monday, full brief every day,
# weekly wrap on Friday
# ─────────────────────────────────────────
def daily_run():
    today = datetime.now().strftime("%A")
    log(f"Daily run triggered - {today}")

    if today == "Monday":
        monday_agenda_sweep()

    if today == "Friday":
        friday_sweep()

    # EMC monitoring runs every day
    daily_monitoring_sweep()


# ─────────────────────────────────────────
# HEALTH CHECK SERVER (required for Railway)
# ─────────────────────────────────────────
class HealthCheck(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"EMC Agent - OK")

    def log_message(self, format, *args):
        pass  # suppress access logs


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheck)
    log(f"Health check server running on port {port}")
    server.serve_forever()

# Base44 Dashboard
BASE44_WEBHOOK = os.environ.get("BASE44_WEBHOOK_URL")
def send_to_dashboard(sweep_type, content):
    if not BASE44_WEBHOOK:
        log("No BASE44_WEBHOOK_URL set - skipping dashboard push")
        return
    try:
        payload = {
            "sweep_type": sweep_type,
            "timestamp": datetime.now().isoformat(),
            "content": content
        }
        r = requests.post(
            BASE44_WEBHOOK,
            json=payload,
            timeout=10
        )
        log(f"Dashboard updated - status {r.status_code}")
    except Exception as e:
        log(f"Dashboard push failed - {e}")


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    log("EMC Intelligence Agent starting...")

    # Start health check server in background
    threading.Thread(target=start_health_server, daemon=True).start()

    # Run once immediately on startup
    daily_run()

    # Schedule daily at 7:30 AM
    schedule.every().day.at("07:30").do(daily_run)

    log("Scheduler running - waiting for next trigger...")
    while True:
        schedule.run_pending()
        time.sleep(60)

"""
student-job-alerts scraper
--------------------------
Fetches student/intern job postings from top tech companies and
reports any newly seen ones since the last run.

Currently supported companies: Amazon, Microsoft, NVIDIA, Apple, Intel, Check Point, Mobileye
(Requires Playwright: Google, Meta, Cisco, IBM, Qualcomm)

How it works:
  1. Each company has its own fetch function that returns a list of Job objects.
  2. run_all_scrapers() calls every company and collects all jobs.
  3. load_seen_jobs() / save_seen_jobs() track which job IDs we've already reported,
     so we only alert on truly new postings.
  4. notify_new_jobs() sends a Telegram message for each new job found.

Usage:
  python scraper.py
"""

import json
import os
import time
from datetime import datetime
# curl_cffi impersonates Chrome's TLS fingerprint, which bypasses Cloudflare bot
# detection on sites like NVIDIA. It is API-compatible with the requests library.
from curl_cffi import requests
from dotenv import load_dotenv

# Load TELEGRAM_TOKEN and TELEGRAM_CHAT_ID from the .env file
load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ── Configuration ────────────────────────────────────────────────────────────

# Keywords used to filter job titles for student-relevant roles.
STUDENT_KEYWORDS = ["intern", "internship", "new grad", "entry level", "student"]

# Jobs whose titles contain any of these are excluded — they target PhD/MSc candidates,
# not BSc students.
DEGREE_EXCLUDE_KEYWORDS = [
    "phd", "ph.d", "doctorate", "doctoral",
    "msc", "m.sc", "masters", "master's", "master ",
    "postdoc", "post-doc", "post doc",
    "research scientist",  # almost always requires a PhD
]

# Jobs must contain at least one of these to be considered CS-relevant.
# This filters out unrelated intern roles (HR, marketing, finance, legal, etc.).
CS_KEYWORDS = [
    "software", "engineer", "developer", "programming",
    "data", "machine learning", "ml", "ai", "artificial intelligence",
    "security", "cyber", "network", "systems", "infrastructure",
    "backend", "frontend", "full stack", "fullstack", "web",
    "algorithm", "computer", "cloud", "devops", "platform",
    "research intern",  # keep research internships (often CS-relevant)
    "product",          # product roles can be relevant for CS students
]

# Only jobs located in Israel will be reported.
ISRAEL_KEYWORDS = ["israel", "tel aviv", "tel-aviv", "haifa", "jerusalem", "herzliya", "beer sheva", "il,"]

# File that persists job IDs we've already seen across runs.
# On a self-hosted runner, SEEN_JOBS_FILE points to a path outside the
# workspace so it survives git checkout on each run.
SEEN_JOBS_FILE = os.getenv("SEEN_JOBS_FILE", "seen_jobs.json")

# Base URL used when building full links to Amazon job listings.
AMAZON_BASE_URL = "https://www.amazon.jobs"

# NVIDIA Workday API — base URL (location facet filter causes intermittent 400s so we
# paginate all intern results and filter Israel client-side instead).
NVIDIA_WORKDAY_URL = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
NVIDIA_WORKDAY_BASE_URL = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"

# Microsoft careers API — discovered by intercepting XHR calls on the careers page.
MICROSOFT_SEARCH_URL = "https://apply.careers.microsoft.com/api/pcsx/search"
MICROSOFT_BASE_URL = "https://jobs.careers.microsoft.com"

# Apple careers — SSR page with job data embedded in window.__staticRouterHydrationData.
# Location code for Israel is "israel-ISR"; keyword search filters all job text.
APPLE_SEARCH_URL = "https://jobs.apple.com/en-us/search?location=israel-ISR&search=intern"
APPLE_BASE_URL   = "https://jobs.apple.com/en-us/details"

# Intel careers — Workday API (no session/CSRF required, unlike NVIDIA).
INTEL_WORKDAY_URL      = "https://intel.wd1.myworkdayjobs.com/wday/cxs/intel/External/jobs"
INTEL_WORKDAY_BASE_URL = "https://intel.wd1.myworkdayjobs.com/en-US/External"

# Check Point careers — SmartRecruiters public REST API.
CHECKPOINT_API_URL  = "https://api.smartrecruiters.com/v1/companies/checkpointsoftwaretechnologies/postings"
CHECKPOINT_BASE_URL = "https://jobs.smartrecruiters.com/CheckPointSoftwareTechnologies"

# Mobileye careers — public JSON API (no auth, returns all jobs in one request).
MOBILEYE_API_URL = "https://careers-api.mbly.co/jobs"


# ── Data model ───────────────────────────────────────────────────────────────

class Job:
    """Represents a single job posting."""

    def __init__(self, job_id: str, title: str, company: str, location: str, url: str, posted: str = "", summary: str = ""):
        """
        Args:
            job_id:   Unique identifier for the job (used to detect duplicates).
            title:    Job title (e.g. "Software Development Engineer Intern").
            company:  Company name (e.g. "Amazon").
            location: City / country of the role.
            url:      Direct link to the job posting.
            posted:   Date the job was posted, as a human-readable string.
            summary:  Short summary of key requirements, shown in the notification.
        """
        self.job_id   = job_id
        self.title    = title
        self.company  = company
        self.location = location
        self.url      = url
        self.posted   = posted
        self.summary  = summary

    def is_student_role(self) -> bool:
        """Return True if the job title contains any student-relevant keyword."""
        title_lower = self.title.lower()
        return any(kw in title_lower for kw in STUDENT_KEYWORDS)

    def is_in_israel(self) -> bool:
        """Return True if the job location is in Israel."""
        location_lower = self.location.lower()
        return any(kw in location_lower for kw in ISRAEL_KEYWORDS)

    def is_bsc_level(self) -> bool:
        """
        Return True if the job is appropriate for a BSc student.

        Excludes roles that explicitly require a PhD or Master's degree,
        and roles in unrelated fields (HR, marketing, finance, etc.).
        """
        title_lower = self.title.lower()
        # Reject PhD / MSc roles
        if any(kw in title_lower for kw in DEGREE_EXCLUDE_KEYWORDS):
            return False
        # Keep only CS-relevant roles
        return any(kw in title_lower for kw in CS_KEYWORDS)

    def age_in_days(self) -> int | None:
        """
        Return how many days ago the job was posted, or None if unparseable.

        Handles multiple formats returned by different job APIs:
          - 'November  4, 2025'   (Amazon)
          - '2025-11-04'          (ISO format, for future scrapers)
          - 'Posted 3 Days Ago'   (NVIDIA / Workday)
          - 'Posted 30+ Days Ago' (NVIDIA / Workday — treated as 30 days)
        """
        text = self.posted.strip()

        # Workday format: "Posted 3 Days Ago" or "Posted 30+ Days Ago"
        if text.lower().startswith("posted"):
            import re
            match = re.search(r"(\d+)\+?\s+days?\s+ago", text, re.IGNORECASE)
            if match:
                return int(match.group(1))

        # Unix timestamp (Microsoft)
        if text.isdigit():
            posted_date = datetime.fromtimestamp(int(text))
            return (datetime.now() - posted_date).days

        # Standard date formats
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
            try:
                posted_date = datetime.strptime(text, fmt)
                return (datetime.now() - posted_date).days
            except ValueError:
                continue

        return None

    def freshness_indicator(self) -> tuple[str, str]:
        """
        Return a color label based on how old the posting is.

        Rules:
          < 7 days  → green  (fresh)
          7-14 days → yellow (getting old)
          > 14 days → red    (stale)

        Returns:
            A tuple of (terminal_color_code, emoji) for use in output.
            Terminal uses ANSI escape codes; Telegram uses colored circle emojis.
        """
        days = self.age_in_days()
        if days is None:
            return ("\033[0m", "⚪")   # unknown — no color
        if days < 7:
            return ("\033[92m", "🟢")  # green
        if days < 14:
            return ("\033[93m", "🟡")  # yellow
        return ("\033[91m", "🔴")      # red

    def __repr__(self) -> str:
        return f"Job({self.company} | {self.title} | {self.location})"


# ── Scrapers ─────────────────────────────────────────────────────────────────

def fetch_amazon_jobs() -> list[Job]:
    """
    Fetch student / intern job postings from Amazon's public search API.

    Amazon exposes a JSON endpoint that accepts query parameters:
      - base_query:     keyword search
      - category_type:  'student-programs' narrows to intern / new-grad roles
      - result_limit:   max number of results per request
      - offset:         for pagination

    Returns:
        A list of Job objects for roles that match student keywords.
    """
    url = (
        "https://www.amazon.jobs/en/search.json"
        "?base_query=software+intern"
        "&category_type=student-programs"
        "&result_limit=50"
        "&offset=0"
    )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10, impersonate="chrome")
        response.raise_for_status()
    except Exception as e:
        print(f"[Amazon] Request failed: {e}")
        return []

    try:
        data = response.json()
    except Exception as e:
        print(f"[Amazon] Failed to parse response: {e}")
        return []
    raw_jobs = data.get("jobs", [])
    jobs     = []

    for item in raw_jobs:
        # Strip HTML tags from basic_qualifications and trim to a short summary
        import re as _re
        raw_quals = item.get("basic_qualifications", "")
        clean_quals = _re.sub(r"<[^>]+>", " ", raw_quals)
        clean_quals = _re.sub(r"\s+", " ", clean_quals).strip()
        summary = clean_quals[:400] + "…" if len(clean_quals) > 400 else clean_quals

        job = Job(
            job_id   = str(item.get("id", "")),
            title    = item.get("title", ""),
            company  = "Amazon",
            location = item.get("location", ""),
            url      = AMAZON_BASE_URL + item.get("job_path", ""),
            posted   = item.get("posted_date", ""),
            summary  = summary,
        )
        # Only keep BSc CS-relevant student roles based in Israel
        if job.is_student_role() and job.is_in_israel() and job.is_bsc_level():
            jobs.append(job)

    return jobs


def _fetch_microsoft_summary(position_id: int) -> tuple[str, str]:
    """
    Fetch the job description for a single Microsoft position and extract
    the qualifications section as a short summary.

    Also returns the public URL for the job listing, which is the correct
    link to share (the search API returns an internal path that redirects
    to the homepage instead of the job page).

    Args:
        position_id: The numeric job ID from the search results.

    Returns:
        A tuple of (summary, public_url). Both are empty strings on failure.
    """
    try:
        r = requests.get(
            f"https://apply.careers.microsoft.com/api/pcsx/position_details"
            f"?position_id={position_id}&domain=microsoft.com&hl=en",
            impersonate="chrome",
            timeout=10,
        )
        r.raise_for_status()
        data = r.json().get("data", {})

        # Strip HTML tags from the job description
        import re as _re
        desc = _re.sub(r"<[^>]+>", " ", data.get("jobDescription", ""))
        desc = _re.sub(r"\s+", " ", desc).strip()

        # Extract from the "Qualifications" section onward
        qual_idx = desc.lower().find("qualif")
        summary = desc[qual_idx:qual_idx + 500] + "…" if qual_idx >= 0 else desc[:400] + "…"

        public_url = data.get("publicUrl", "")
        return summary, public_url

    except Exception as e:
        print(f"[Microsoft] Summary fetch failed for {position_id}: {e}")
        return "", ""


def fetch_microsoft_jobs() -> list[Job]:
    """
    Fetch student / intern job postings from Microsoft's careers API.

    Microsoft's careers site (jobs.careers.microsoft.com) loads job data via
    an internal API at apply.careers.microsoft.com. This endpoint was discovered
    by intercepting XHR network calls on the careers page using Playwright.

    For each matching job we fetch position_details to get:
      - A qualifications summary for the Telegram notification.
      - The correct public URL (the search API returns an internal path that
        redirects to the homepage rather than the job page).

    Returns:
        A list of Job objects for student roles located in Israel.
    """
    params = {
        "domain": "microsoft.com",
        "query": "intern",
        "location": "Israel",
        "start": "0",
        "num": "50",
    }

    try:
        response = requests.get(
            MICROSOFT_SEARCH_URL,
            params=params,
            impersonate="chrome",
            timeout=10,
        )
        response.raise_for_status()
    except Exception as e:
        print(f"[Microsoft] Request failed: {e}")
        return []

    try:
        positions = response.json().get("data", {}).get("positions", [])
    except Exception as e:
        print(f"[Microsoft] Failed to parse response: {e}")
        return []
    jobs = []

    for item in positions:
        location = ", ".join(item.get("locations", []))
        position_id = item.get("id", "")

        job = Job(
            job_id   = f"microsoft_{position_id}",
            title    = item.get("name", ""),
            company  = "Microsoft",
            location = location,
            posted   = datetime.fromtimestamp(item["postedTs"]).strftime("%B %d, %Y") if item.get("postedTs") else "",
            url      = "",      # filled in below after fetching details
            summary  = "",      # filled in below after fetching details
        )

        if job.is_student_role() and job.is_bsc_level():
            # Fetch qualifications summary and correct public URL
            summary, public_url = _fetch_microsoft_summary(position_id)
            job.summary = summary
            job.url = public_url or (MICROSOFT_BASE_URL + item.get("positionUrl", ""))
            jobs.append(job)

    return jobs


def fetch_apple_jobs() -> list[Job]:
    """
    Fetch student / intern job postings from Apple's careers site.

    Apple renders job data server-side and embeds it in the page HTML as
    window.__staticRouterHydrationData (a JSON-encoded string inside a <script>
    tag). No separate API call is needed — a single GET request returns all
    the data we need.

    The search URL uses:
      - location=israel-ISR   narrow to Israel postings
      - search=intern         keyword match across title + description

    Returns:
        A list of Job objects for student roles located in Israel.
    """
    import re as _re

    try:
        response = requests.get(
            APPLE_SEARCH_URL,
            impersonate="chrome",
            timeout=15,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        response.raise_for_status()
    except Exception as e:
        print(f"[Apple] Request failed: {e}")
        return []

    # Extract the SSR hydration JSON from the page HTML.
    # Apple embeds all page data as: window.__staticRouterHydrationData = JSON.parse("...");
    m = _re.search(
        r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\("(.+?)"\);\s*</script>',
        response.text,
        _re.DOTALL,
    )
    if not m:
        print("[Apple] Could not find hydration data in page HTML.")
        return []

    # The JSON is double-encoded (a JSON string containing escaped JSON).
    # Decode the outer string escaping first, then parse the inner JSON.
    try:
        raw = m.group(1)
        decoded = raw.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")
        data = json.loads(decoded)
    except Exception as e:
        print(f"[Apple] Failed to parse hydration JSON: {e}")
        return []

    raw_jobs = data.get("loaderData", {}).get("search", {}).get("searchResults", [])
    jobs = []

    for item in raw_jobs:
        position_id = item.get("positionId", "")
        slug        = item.get("transformedPostingTitle", "")

        # Build location string from the locations list (usually one entry)
        location_parts = [
            loc.get("name") or loc.get("countryName", "")
            for loc in item.get("locations", [])
        ]
        location = ", ".join(filter(None, location_parts))

        # Strip HTML tags from the job summary (Apple occasionally uses <b> etc.)
        raw_summary = item.get("jobSummary", "")
        summary = _re.sub(r"<[^>]+>", " ", raw_summary)
        summary = _re.sub(r"\s+", " ", summary).strip()

        job = Job(
            job_id   = f"apple_{position_id}",
            title    = item.get("postingTitle", ""),
            company  = "Apple",
            location = location,
            url      = f"{APPLE_BASE_URL}/{position_id}/{slug}",
            posted   = item.get("postingDate", ""),   # "Apr 29, 2026" — parsed by %b %d, %Y
            summary  = summary,
        )

        if job.is_student_role() and job.is_bsc_level():
            jobs.append(job)

    return jobs


def _workday_playwright_fetch(
    page_url: str,
    api_url: str,
    base_url: str,
    label: str,
    id_prefix: str,
) -> list[Job]:
    """
    Fetch Workday jobs using a real Chromium browser to bypass Cloudflare.

    curl_cffi mimics the TLS fingerprint but can't execute Cloudflare's JS
    challenge. Playwright runs real Chromium, so the challenge is solved
    automatically. After the page loads we use the established browser session
    (cookies + CSRF token) to call the JSON API directly.
    """
    from playwright.sync_api import sync_playwright

    raw_jobs: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        # Hide the navigator.webdriver flag that Cloudflare checks for headless bots
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        try:
            page.goto(page_url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            print(f"[{label}] Page load failed: {e}")
            context.close()
            browser.close()
            return []

        cookies = context.cookies()
        csrf = next((c["value"] for c in cookies if c["name"] == "CALYPSO_CSRF_TOKEN"), "")

        offset, limit = 0, 100
        while True:
            try:
                # Use page.evaluate() so the fetch runs inside the browser —
                # this sends all session cookies and Origin/Referer headers
                # automatically, exactly like a real XHR from the Workday page.
                result = page.evaluate("""
                    async ([url, csrf, body]) => {
                        const headers = {'Content-Type': 'application/json'};
                        if (csrf) headers['X-Workday-Client-CSRF-Token'] = csrf;
                        const r = await fetch(url, {method: 'POST', headers, body});
                        return {status: r.status, data: await r.json()};
                    }
                """, [api_url, csrf, json.dumps({"limit": limit, "offset": offset, "searchText": "intern", "appliedFacets": {}})])
                if result["status"] != 200:
                    print(f"[{label}] API returned {result['status']}")
                    break
                data = result["data"]
            except Exception as e:
                print(f"[{label}] API request failed: {e}")
                break

            page_jobs = data.get("jobPostings", [])
            total = data.get("total", 0)
            if not page_jobs:
                break
            raw_jobs.extend(page_jobs)
            offset += len(page_jobs)
            if offset >= total:
                break

        context.close()
        browser.close()

    jobs = []
    for item in raw_jobs:
        loc = item.get("locationsText", "")
        if not any(kw in loc.lower() for kw in ISRAEL_KEYWORDS):
            continue

        job_id = item.get("bulletFields", [""])[0] or item.get("externalPath", "")
        job = Job(
            job_id   = f"{id_prefix}_{job_id}",
            title    = item.get("title", ""),
            company  = label,
            location = loc,
            url      = base_url + item.get("externalPath", ""),
            posted   = item.get("postedOn", ""),
        )
        if job.is_student_role() and job.is_bsc_level():
            jobs.append(job)

    return jobs


def fetch_intel_jobs() -> list[Job]:
    """Fetch Intel intern jobs from Workday using Playwright to bypass Cloudflare."""
    return _workday_playwright_fetch(
        page_url  = "https://intel.wd1.myworkdayjobs.com/External",
        api_url   = INTEL_WORKDAY_URL,
        base_url  = INTEL_WORKDAY_BASE_URL,
        label     = "Intel",
        id_prefix = "intel",
    )


def fetch_checkpoint_jobs() -> list[Job]:
    """
    Fetch student / intern job postings from Check Point's SmartRecruiters board.

    Check Point uses SmartRecruiters as their ATS. The public API requires no
    authentication and returns JSON with full location and date information.

    Israel is identified by location.country == "IL". The releasedDate field
    is ISO 8601 (e.g. "2025-01-15T10:00:00.000Z"); we slice to "YYYY-MM-DD"
    which age_in_days() already handles.

    Returns:
        A list of Job objects for student roles located in Israel.
    """
    raw_jobs: list[dict] = []
    offset, limit = 0, 100

    while True:
        try:
            response = requests.get(
                CHECKPOINT_API_URL,
                params={"limit": limit, "offset": offset},
                impersonate="chrome",
                timeout=15,
            )
            response.raise_for_status()
        except Exception as e:
            print(f"[Check Point] Request failed: {e}")
            break

        data  = response.json()
        total = data.get("totalFound", 0)
        page  = data.get("content", [])
        if not page:
            break
        raw_jobs.extend(page)
        offset += len(page)
        if offset >= total:
            break
        time.sleep(0.5)

    jobs = []
    for item in raw_jobs:
        loc_obj  = item.get("location", {})
        country  = loc_obj.get("country", "")
        city     = loc_obj.get("city", "")
        location = ", ".join(filter(None, [city, country]))

        if country.lower() != "il" and not any(kw in location.lower() for kw in ISRAEL_KEYWORDS):
            continue

        released = item.get("releasedDate", "")
        posted   = released[:10] if released else ""   # "2025-01-15T..." → "2025-01-15"
        job_id   = item.get("id", "")

        job = Job(
            job_id   = f"checkpoint_{job_id}",
            title    = item.get("name", ""),
            company  = "Check Point",
            location = location,
            url      = f"{CHECKPOINT_BASE_URL}/{job_id}",
            posted   = posted,
        )
        if job.is_student_role() and job.is_bsc_level():
            jobs.append(job)

    return jobs


def fetch_mobileye_jobs() -> list[Job]:
    """
    Fetch job postings from Mobileye's public careers API.

    Mobileye exposes a flat JSON array at careers-api.mbly.co/jobs with no
    authentication. One request returns all ~130 listings; ~120 are in Israel.
    The applyUrl points to their Lever-hosted application page.

    createdAt is a Unix timestamp in milliseconds; we convert to YYYY-MM-DD
    for compatibility with age_in_days().

    Returns:
        A list of Job objects for student roles located in Israel.
    """
    try:
        response = requests.get(MOBILEYE_API_URL, impersonate="chrome", timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"[Mobileye] Request failed: {e}")
        return []

    try:
        raw = response.json()
    except Exception as e:
        print(f"[Mobileye] Failed to parse response: {e}")
        return []
    jobs = []
    for item in raw:
        categories = item.get("categories", {})
        location   = categories.get("location", "")

        if not any(kw in location.lower() for kw in ISRAEL_KEYWORDS):
            continue

        created_ms = item.get("createdAt", 0)
        posted     = datetime.fromtimestamp(created_ms / 1000).strftime("%Y-%m-%d") if created_ms else ""

        raw_desc = item.get("descriptionBodyPlain", "")
        summary  = raw_desc[:400] + "…" if len(raw_desc) > 400 else raw_desc

        job = Job(
            job_id   = f"mobileye_{item.get('id', '')}",
            title    = item.get("text", ""),
            company  = "Mobileye",
            location = location,
            url      = item.get("applyUrl", ""),
            posted   = posted,
            summary  = summary,
        )
        if job.is_student_role() and job.is_bsc_level():
            jobs.append(job)

    return jobs


def fetch_nvidia_jobs() -> list[Job]:
    """Fetch NVIDIA intern jobs from Workday using Playwright to bypass Cloudflare."""
    return _workday_playwright_fetch(
        page_url  = "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
        api_url   = NVIDIA_WORKDAY_URL,
        base_url  = NVIDIA_WORKDAY_BASE_URL,
        label     = "NVIDIA",
        id_prefix = "nvidia",
    )


# ── Persistence (seen-jobs tracking) ─────────────────────────────────────────

def load_seen_jobs() -> set[str]:
    """
    Load the set of job IDs we have already reported.

    Returns an empty set if the file does not exist yet (first run).
    """
    if not os.path.exists(SEEN_JOBS_FILE):
        return set()

    with open(SEEN_JOBS_FILE, "r") as f:
        return set(json.load(f))


def save_seen_jobs(seen: set[str]) -> None:
    """
    Persist the set of seen job IDs to disk so the next run knows what's new.

    Args:
        seen: The complete set of job IDs seen so far.
    """
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(list(seen), f)


# ── Notifications ────────────────────────────────────────────────────────────

def _format_bullets(summary: str, max_bullets: int = 4, max_len: int = 80) -> str:
    """
    Convert a raw qualifications string into a short bullet-point list.

    Splits on common delimiters (hyphens, semicolons, newlines), takes the
    first max_bullets non-empty items, and trims each to max_len characters.

    Args:
        summary:     Raw qualifications text.
        max_bullets: Maximum number of bullet points to return.
        max_len:     Maximum character length per bullet.

    Returns:
        A string of bullet points separated by newlines, e.g. "• ...\n• ..."
    """
    import re as _re

    # Split on common list delimiters
    parts = _re.split(r"\s*[-•]\s+|\n|;\s*", summary)
    bullets = []
    for part in parts:
        part = part.strip()
        # Skip very short fragments, headers like "Required Qualifications", etc.
        if len(part) < 15 or part.lower() in ("required qualifications", "qualifications"):
            continue
        # Trim long items
        trimmed = part[:max_len] + "…" if len(part) > max_len else part
        bullets.append(f"• {trimmed}")
        if len(bullets) >= max_bullets:
            break

    return "\n".join(bullets)

def send_telegram_message(text: str) -> None:
    """
    Send a message to your Telegram chat via the bot.

    Uses the Telegram Bot API's sendMessage endpoint.
    Credentials are read from the .env file (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID).

    Args:
        text: The message text to send. Supports HTML formatting.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=10, impersonate="chrome")
        response.raise_for_status()
    except Exception as e:
        print(f"[Telegram] Failed to send message: {e}")


def notify_new_jobs(new_jobs: list[Job]) -> None:
    """
    Send a Telegram alert for each new job, and print a summary to the terminal.

    Each job gets its own message so your phone shows a clear, readable notification.
    Also prints to the terminal so you can see what was sent when running locally.

    Args:
        new_jobs: Jobs that have not been reported in a previous run.
    """
    if not new_jobs:
        print("No new student jobs found.")
        return

    print(f"\n{'='*60}")
    print(f"  {len(new_jobs)} NEW STUDENT JOB(S) FOUND")
    print(f"{'='*60}\n")

    RESET = "\033[0m"

    for job in new_jobs:
        color, emoji = job.freshness_indicator()
        days         = job.age_in_days()
        age_label    = f"{days} days ago" if days is not None else "unknown date"

        # Terminal output — job title is colored by freshness
        print(f"  Company  : {job.company}")
        print(f"  Title    : {color}{job.title}{RESET}")
        print(f"  Location : {job.location}")
        print(f"  Posted   : {color}{job.posted} ({age_label}){RESET}")
        if job.summary:
            print(f"  Summary  : {job.summary[:200]}")
        print(f"  Link     : {job.url}")
        print(f"  {'-'*56}")

        # Build requirements block — formatted as bullet points
        if job.summary:
            bullets = _format_bullets(job.summary)
            summary_block = f"\n\n📋 <b>Requirements:</b>\n{bullets}" if bullets else ""
        else:
            summary_block = ""

        # Telegram message — concise, bullet-formatted
        message = (
            f"{emoji} <b>{job.company} — {job.title}</b>\n"
            f"📍 {job.location}\n"
            f"📅 {job.posted} <i>({age_label})</i>"
            f"{summary_block}\n\n"
            f"🔗 <a href=\"{job.url}\">View Job</a>"
        )
        send_telegram_message(message)


# ── Main ─────────────────────────────────────────────────────────────────────

def run_all_scrapers() -> list[Job]:
    """
    Run every company scraper and return the combined list of jobs.

    To add a new company later, define a fetch_<company>_jobs() function
    and add it to the list below.
    """
    all_jobs = []
    scrapers = [
        fetch_amazon_jobs,
        fetch_microsoft_jobs,
        fetch_nvidia_jobs,
        # fetch_google_jobs,   # TODO: blocks headless browsers, protobuf API
        # fetch_meta_jobs,     # TODO: very few Israel intern jobs, requires Playwright session
        fetch_apple_jobs,
        fetch_intel_jobs,
        fetch_checkpoint_jobs,
        fetch_mobileye_jobs,
        # fetch_google_jobs,    # requires Playwright (client-side rendered)
        # fetch_meta_jobs,      # requires Playwright (client-side rendered)
        # fetch_cisco_jobs,     # Phenom People, requires Playwright
        # fetch_ibm_jobs,       # AWS WAF + client-side rendering, requires Playwright
        # fetch_qualcomm_jobs,  # custom SPA, API returns 401
    ]

    for scraper in scrapers:
        try:
            company_jobs = scraper()
            print(f"[{scraper.__name__}] Found {len(company_jobs)} student roles.")
            all_jobs.extend(company_jobs)
        except Exception as e:
            print(f"[{scraper.__name__}] CRASHED: {e}")

    return all_jobs


def main():
    """
    Entry point — run scrapers, compare against seen jobs, report new ones.

    Flow:
      1. Load previously seen job IDs from disk.
      2. Fetch current job postings from all companies.
      3. Find jobs whose IDs we haven't seen before.
      4. Print them (notifications will be added here later).
      5. Save updated seen-jobs list to disk.
    """
    print("Checking for new student job postings...\n")

    seen_ids  = load_seen_jobs()
    all_jobs  = run_all_scrapers()

    # Filter to only jobs we haven't reported yet
    new_jobs  = [j for j in all_jobs if j.job_id not in seen_ids]

    notify_new_jobs(new_jobs)

    # Update the seen-jobs file so we don't alert on these again
    seen_ids.update(j.job_id for j in all_jobs)
    save_seen_jobs(seen_ids)


if __name__ == "__main__":
    main()

"""
student-job-alerts scraper
--------------------------
Fetches student/intern job postings from top tech companies and
reports any newly seen ones since the last run.

Currently supported companies: Amazon
(More will be added: Microsoft, Google, Meta, Apple)

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
from datetime import datetime
import requests
from dotenv import load_dotenv

# Load TELEGRAM_TOKEN and TELEGRAM_CHAT_ID from the .env file
load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ── Configuration ────────────────────────────────────────────────────────────

# Keywords used to filter job titles for student-relevant roles.
STUDENT_KEYWORDS = ["intern", "internship", "new grad", "entry level", "student"]

# Only jobs located in Israel will be reported.
ISRAEL_KEYWORDS = ["israel", "tel aviv", "tel-aviv", "haifa", "jerusalem", "herzliya", "beer sheva", "il,"]

# File that persists job IDs we've already seen across runs.
SEEN_JOBS_FILE = "seen_jobs.json"

# Base URL used when building full links to Amazon job listings.
AMAZON_BASE_URL = "https://www.amazon.jobs"


# ── Data model ───────────────────────────────────────────────────────────────

class Job:
    """Represents a single job posting."""

    def __init__(self, job_id: str, title: str, company: str, location: str, url: str, posted: str = ""):
        """
        Args:
            job_id:   Unique identifier for the job (used to detect duplicates).
            title:    Job title (e.g. "Software Development Engineer Intern").
            company:  Company name (e.g. "Amazon").
            location: City / country of the role.
            url:      Direct link to the job posting.
            posted:   Date the job was posted, as a human-readable string.
        """
        self.job_id   = job_id
        self.title    = title
        self.company  = company
        self.location = location
        self.url      = url
        self.posted   = posted

    def is_student_role(self) -> bool:
        """Return True if the job title contains any student-relevant keyword."""
        title_lower = self.title.lower()
        return any(kw in title_lower for kw in STUDENT_KEYWORDS)

    def is_in_israel(self) -> bool:
        """Return True if the job location is in Israel."""
        location_lower = self.location.lower()
        return any(kw in location_lower for kw in ISRAEL_KEYWORDS)

    def age_in_days(self) -> int | None:
        """
        Return how many days ago the job was posted, or None if unparseable.

        Tries two common date formats returned by job APIs:
          - 'November  4, 2025'  (Amazon's format)
          - '2025-11-04'         (ISO format, for future scrapers)
        """
        for fmt in ("%B %d, %Y", "%Y-%m-%d"):
            try:
                posted_date = datetime.strptime(self.posted.strip(), fmt)
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
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"[Amazon] Request failed: {e}")
        return []

    data     = response.json()
    raw_jobs = data.get("jobs", [])
    jobs     = []

    for item in raw_jobs:
        job = Job(
            job_id   = str(item.get("id", "")),
            title    = item.get("title", ""),
            company  = "Amazon",
            location = item.get("location", ""),
            url      = AMAZON_BASE_URL + item.get("job_path", ""),
            posted   = item.get("posted_date", ""),
        )
        # Only keep student roles based in Israel
        if job.is_student_role() and job.is_in_israel():
            jobs.append(job)

    return jobs


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
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
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
        print(f"  Link     : {job.url}")
        print(f"  {'-'*56}")

        # Telegram message — uses colored circle emoji since Telegram has no text colors
        message = (
            f"🎓 <b>New Student Role at {job.company}</b>\n\n"
            f"<b>{job.title}</b>\n"
            f"📍 {job.location}\n"
            f"📅 Posted: {job.posted} {emoji} <i>({age_label})</i>\n\n"
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
        # fetch_microsoft_jobs,  # coming soon
        # fetch_google_jobs,     # coming soon
        # fetch_meta_jobs,       # coming soon
        # fetch_apple_jobs,      # coming soon
    ]

    for scraper in scrapers:
        company_jobs = scraper()
        print(f"[{scraper.__name__}] Found {len(company_jobs)} student roles.")
        all_jobs.extend(company_jobs)

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

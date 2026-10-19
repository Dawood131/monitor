"""
Job Monitor - checks career pages for frontend/React/MERN openings
and emails you when something new shows up.

Uses a real headless browser (Playwright/Chromium) so JavaScript-rendered
career pages (React, Next.js, Angular SPAs — the majority of modern sites)
are actually loaded, not just their empty raw HTML.

Free automation pattern: this script is meant to be run on a schedule
by GitHub Actions (free), not kept running on your own machine.
See README.md for setup.
"""

import csv
import json
import os
import re
import smtplib
import sys
import urllib.request
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent
COMPANIES_FILE = BASE_DIR / "companies.json"
STATE_FILE = BASE_DIR / "seen_jobs.json"

# Keywords that mean "this looks like a job I want"
KEYWORDS = [
    r"front[\s-]?end\s*(developer|engineer|dev\b)",
    r"react(\.?js)?\s*(developer|engineer)",
    r"next\.?js",
    r"mern",
    r"full[\s-]?stack\s*(web\s*)?(developer|engineer|development)",
    r"ui\s*(developer|engineer)",
    r"(javascript|typescript)\s*(developer|engineer)",
    r"web\s*(developer|engineer)",
]
KEYWORD_RE = re.compile("|".join(KEYWORDS), re.IGNORECASE)

# Words that usually mean "not for me" — skip these even if a keyword matched
EXCLUDE_RE = re.compile(
    r"senior|sr\.|lead|principal|10\+\s*years|8\+\s*years|7\+\s*years|6\+\s*years|5\+\s*years",
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def fetch_rendered_html(browser, url: str, wait_ms: int = 4000) -> str:
    """Load a page in a real headless browser and return the HTML *after*
    JavaScript has run. Also: ignores expired/bad SSL certificates (some small
    company sites have them), waits for network to settle, scrolls once to
    trigger lazy-loaded lists, and appends iframe content (Workable / Zoho
    Recruit / ApplyToJob style career widgets live inside iframes)."""
    context = browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
    page = context.new_page()
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        page.wait_for_timeout(wait_ms)
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1000)
        except Exception:
            pass
        html = page.content()
        for frame in page.frames[1:]:
            try:
                html += "\n" + frame.content()
            except Exception:
                pass
    finally:
        context.close()
    return html


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def extract_candidate_lines(html: str):
    """Pull short, heading-like lines of text out of a page — these are
    where job titles usually live (h1-h4, links, list items)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    lines = set()
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "a", "li", "span", "p"]):
        text = tag.get_text(strip=True)
        if text and 3 <= len(text) <= 120:
            lines.add(text)
    return lines


def find_matching_jobs(lines):
    matches = []
    for line in lines:
        if KEYWORD_RE.search(line) and not EXCLUDE_RE.search(line):
            matches.append(line)
    return sorted(set(matches))


BLOCK_MARKERS = (
    "verify you are human", "just a moment", "access denied", "unusual traffic",
    "are you a robot", "checking your browser", "please complete the security check",
    "enable javascript and cookies to continue",
)


def block_reason(html: str, line_count: int):
    """Return a short reason string if the page looks blocked/empty, else None.
    Only looks at VISIBLE text (scripts/styles removed), so a reCAPTCHA script
    on a normal contact form no longer causes a false alarm."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible = soup.get_text(" ", strip=True).lower()
    for m in BLOCK_MARKERS:
        if m in visible[:3000]:
            return f"bot-check text found: '{m}'"
    if line_count < 15:
        return f"only {line_count} text lines (empty shell / JS portal / iframe)"
    return None


# ---------------------------------------------------------------------------
# Jooble aggregator (official free API) - catches jobs from companies that are
# NOT in companies.json. Needs a free key from https://jooble.org/api/about
# stored as the GitHub secret JOOBLE_API_KEY. Edit the lists below freely.
# ---------------------------------------------------------------------------
JOOBLE_QUERIES = [
    "frontend developer",
    "react developer",
]
JOOBLE_LOCATIONS = ["Lahore"]

# The free key has a request quota (500), so Jooble is only queried on a few
# runs per day (UTC hours below -> 1 run/day x 2 queries = ~2 requests/day).
# A manual "Run workflow" always queries it, so you can test any time.
JOOBLE_RUN_HOURS_UTC = {6, 7}

# How many result pages to read per query (each page = 1 request).
JOOBLE_PAGES = 2

# Company-by-company search (Jooble's `companysearch` mode) for your watchlist.
# 0 = OFF (default). Each company costs 1 request, so only turn this on after
# Jooble raises your quota. e.g. 10 = check the next 10 watchlist companies per
# daily run, cycling through the whole list.
JOOBLE_COMPANY_BATCH = 0

# Titles accepted for WATCHLIST companies (looser than KEYWORD_RE, because for
# companies you care about you want to see more roles).
WATCH_ROLE_RE = re.compile(
    r"front[\s-]?end|react|mern|next\.?js|full[\s-]?stack|web|ui\s*(developer|engineer)|"
    r"javascript|typescript|software\s*(engineer|developer)",
    re.IGNORECASE,
)
_NOISE_RE = re.compile(
    r"\b(pvt|private|limited|ltd|llc|inc|technologies|technology|solutions|software|systems|"
    r"services|group|co|company|lahore|branch|house|studios?)\b"
)

TAG_RE = re.compile(r"<[^>]+>")


def clean_text(t) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub("", t or "")).strip()


def norm_company(name: str) -> str:
    name = re.sub(r"\(.*?\)", " ", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", "", _NOISE_RE.sub(" ", name))


def load_watchlist():
    """Company names to prioritise: the 'Company' column of master.csv if it
    exists (your big sheet), plus everything in companies.json."""
    names = [c["name"] for c in load_json(COMPANIES_FILE, [])]
    master = BASE_DIR / "master.csv"
    if master.exists():
        try:
            text = master.read_text(encoding="utf-8-sig")
            try:
                dialect = csv.Sniffer().sniff(text[:3000], delimiters=",\t;")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(text.splitlines(), dialect=dialect)
            col = next((h for h in (reader.fieldnames or []) if h.strip().lower() == "company"), None)
            if col:
                names += [(r.get(col) or "").strip() for r in reader]
        except Exception as e:
            print(f"[jooble] could not read master.csv: {e}")
    unique, seen_norm = [], set()
    for n in names:
        k = norm_company(n)
        if n and k and k not in seen_norm:
            seen_norm.add(k)
            unique.append(n)
    return unique, seen_norm


def on_watchlist(company: str, watch_norm: set) -> bool:
    nj = norm_company(company)
    if not nj:
        return False
    if nj in watch_norm:
        return True
    return any(min(len(w), len(nj)) >= 8 and (w in nj or nj in w) for w in watch_norm)


def fetch_jooble(api_key: str, keywords: str, location: str, page: int = 1, companysearch: bool = False):
    payload = {"keywords": keywords, "location": location, "page": str(page)}
    if companysearch:
        payload["companysearch"] = "true"
    req = urllib.request.Request(
        f"https://jooble.org/api/{api_key}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8")).get("jobs", [])


def check_jooble(seen: dict, new_findings: dict, job_links: dict):
    api_key = os.environ.get("JOOBLE_API_KEY")
    if not api_key:
        print("[jooble] JOOBLE_API_KEY not set - skipping aggregator search")
        return

    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not manual and datetime.now(timezone.utc).hour not in JOOBLE_RUN_HOURS_UTC:
        print("[jooble] skipped this run (only queried ~1x/day to save API quota)")
        return

    watch_names, watch_norm = load_watchlist()
    print(f"[jooble] watchlist: {len(watch_names)} companies")
    already = set(seen.get("Jooble", []))
    found = {}  # "Company | Title" -> (company, title, link, starred)

    def consider(j, force_watch=False):
        title = clean_text(j.get("title"))
        company = clean_text(j.get("company")) or "Unknown company"
        if not title or EXCLUDE_RE.search(title):
            return
        starred = force_watch or on_watchlist(company, watch_norm)
        ok = (WATCH_ROLE_RE.search(title) if starred else KEYWORD_RE.search(title))
        if ok:
            found[f"{company} | {title}"] = (company, title, j.get("link") or "", starred)

    # 1) broad keyword searches (a few pages each)
    for q in JOOBLE_QUERIES:
        for loc in JOOBLE_LOCATIONS:
            for page in range(1, JOOBLE_PAGES + 1):
                try:
                    jobs = fetch_jooble(api_key, q, loc, page=page)
                except Exception as e:
                    print(f"[jooble] '{q}' / {loc} p{page} failed: {e}")
                    break
                print(f"[jooble] '{q}' / {loc} p{page}: {len(jobs)} results")
                if page == 1:
                    for j in jobs[:6]:  # sample titles, so filter problems are visible in logs
                        print(f"[jooble]    sample: {clean_text(j.get('title'))} @ {clean_text(j.get('company'))}")
                for j in jobs:
                    consider(j)
                if not jobs:
                    break

    # 2) optional: company-by-company search over a rotating slice of the watchlist
    if JOOBLE_COMPANY_BATCH > 0 and watch_names:
        start = int((seen.get("_jooble_cursor") or ["0"])[0]) % len(watch_names)
        batch = [watch_names[(start + i) % len(watch_names)] for i in range(min(JOOBLE_COMPANY_BATCH, len(watch_names)))]
        seen["_jooble_cursor"] = [str((start + len(batch)) % len(watch_names))]
        for name in batch:
            try:
                jobs = fetch_jooble(api_key, name, JOOBLE_LOCATIONS[0], companysearch=True)
            except Exception as e:
                print(f"[jooble] company search '{name}' failed: {e}")
                continue
            hits = [j for j in jobs if norm_company(clean_text(j.get("company"))) == norm_company(name)
                    or on_watchlist(clean_text(j.get("company")), watch_norm)]
            print(f"[jooble] company '{name}': {len(jobs)} results, {len(hits)} from that company")
            for j in hits:
                consider(j, force_watch=True)

    fresh = 0
    for key, (company, title, link, starred) in found.items():
        if key in already:
            continue
        label = f"{'★ ' if starred else ''}{company} (via Jooble)"
        new_findings.setdefault(label, []).append(title)
        job_links[(label, title)] = link
        fresh += 1
    seen["Jooble"] = sorted(already | set(found))  # cumulative, like company pages
    starred_n = sum(1 for v in found.values() if v[3])
    print(f"[jooble] {len(found)} matching jobs ({starred_n} from watchlist companies), {fresh} new")


def build_html_body(new_findings: dict, company_urls: dict, job_links: dict = None) -> str:
    """new_findings: {company_name: [job strings]}
    company_urls:  {company_name: career_page_url}
    Produces a clean HTML email with each company name linked to its
    career page and jobs listed as bullets underneath.
    """
    job_links = job_links or {}
    total = sum(len(v) for v in new_findings.values())

    rows = []
    for company, jobs in new_findings.items():
        url = company_urls.get(company)
        items = []
        for job in jobs:
            link = job_links.get((company, job))
            label = (f'<a href="{escape(link)}" style="color:#1a73e8;">{escape(job)}</a>'
                     if link else escape(job))
            items.append(f"<li style='margin:4px 0;'>{label}</li>")
        job_items = "".join(items)
        if url:
            head = (f'<a href="{escape(url)}" style="font-size:16px;font-weight:bold;'
                    f'color:#1a73e8;text-decoration:none;">{escape(company)} &rarr;</a>')
        else:
            head = f'<span style="font-size:16px;font-weight:bold;">{escape(company)}</span>'
        rows.append(f"""
        <div style="margin-bottom:20px;">
          {head}
          <ul style="margin:6px 0 0 0;padding-left:20px;">
            {job_items}
          </ul>
        </div>
        """)

    html = f"""\
    <html>
      <body style="font-family:Arial,Helvetica,sans-serif;color:#222;line-height:1.5;">
        <h2 style="margin-bottom:4px;">🎯 {total} new job posting(s) found</h2>
        <p style="color:#666;margin-top:0;">Click a company or job title to open it.</p>
        {''.join(rows)}
        <hr style="border:none;border-top:1px solid #eee;margin-top:24px;">
        <p style="color:#999;font-size:12px;">Sent automatically by your job monitor script.</p>
      </body>
    </html>
    """
    return html


def build_plaintext_body(new_findings: dict, company_urls: dict, job_links: dict = None) -> str:
    """Plain-text fallback for email clients that don't render HTML."""
    lines = ["New matching postings found:\n"]
    for company, jobs in new_findings.items():
        url = company_urls.get(company, "")
        lines.append(f"\n{company} ({url}):" if url else f"\n{company}:")
        for job in jobs:
            link = (job_links or {}).get((company, job))
            lines.append(f"  - {job}" + (f"  {link}" if link else ""))
    return "\n".join(lines)


def send_email(new_findings: dict, company_urls: dict, job_links: dict = None):
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    to_email = os.environ.get("TO_EMAIL", smtp_user)

    if not smtp_user or not smtp_pass:
        print("SMTP_USER / SMTP_PASS not set — skipping email, printing instead.")
        print(json.dumps(new_findings, indent=2, ensure_ascii=False))
        return

    total = sum(len(v) for v in new_findings.values())

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Job alert: {total} new posting(s)"
    msg["From"] = smtp_user
    msg["To"] = to_email

    # Attach plain text first, HTML second — email clients prefer the last
    # part that they can render, so HTML is used when supported.
    msg.attach(MIMEText(build_plaintext_body(new_findings, company_urls, job_links), "plain", "utf-8"))
    msg.attach(MIMEText(build_html_body(new_findings, company_urls, job_links), "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_email], msg.as_string())
    print(f"Email sent to {to_email}")


def main():
    companies = load_json(COMPANIES_FILE, [])
    seen = load_json(STATE_FILE, {})  # {company_name: [job strings already notified]}

    new_findings = {}
    suspicious = []
    job_links = {}
    company_urls = {c["name"]: c["url"] for c in companies}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for company in companies:
                name, url = company["name"], company["url"]
                try:
                    html = fetch_rendered_html(browser, url)
                except Exception as e:
                    print(f"[skip] {name}: could not load ({e})")
                    continue

                lines = extract_candidate_lines(html)
                bot_wall = block_reason(html, len(lines) + 99)  # marker check only
                if len(lines) < 15 and not bot_wall:  # still loading? retry once, slower
                    try:
                        html = fetch_rendered_html(browser, url, wait_ms=10000)
                        lines = extract_candidate_lines(html)
                    except Exception as e:
                        print(f"[retry-fail] {name}: {e}")
                matches = find_matching_jobs(lines)

                already_seen = set(seen.get(name, []))
                fresh = [m for m in matches if m not in already_seen]

                reason = block_reason(html, len(lines))
                if reason:
                    suspicious.append(f"{name} -> {reason}")
                    print(f"[WARN] {name}: {reason}")

                if fresh:
                    new_findings[name] = fresh
                    print(f"[NEW] {name}: {fresh}")
                else:
                    print(f"[ok] {name}: no new matches ({len(matches)} on page, {len(lines)} lines scanned)")

                # CUMULATIVE: never forget a job once seen. This is what stops
                # repeat emails when a page loads half-empty on some runs.
                seen[name] = sorted(already_seen | set(matches))
        finally:
            browser.close()

    check_jooble(seen, new_findings, job_links)
    new_findings = dict(sorted(new_findings.items(), key=lambda kv: not kv[0].startswith("★")))

    if suspicious:
        print("\n=== Pages that look blocked/empty (check these URLs) ===")
        for n in suspicious:
            print(f"  - {n}")

    STATE_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")

    if new_findings:
        send_email(new_findings, company_urls, job_links)
    else:
        print("No new matches this run.")


if __name__ == "__main__":
    sys.exit(main())

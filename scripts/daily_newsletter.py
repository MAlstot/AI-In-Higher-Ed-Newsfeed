"""
Daily AI Newsletter generator.

Fetches recent items from a curated list of RSS feeds, asks Claude to select
2-3 quick reads + 1-2 peer-reviewed articles tailored to the user's research
context, and emails the result (plus an .ics calendar invite for a
7:00-7:30 AM reading block) to RECIPIENT_EMAIL.

Designed to run unattended from GitHub Actions once per day. All secrets
come from environment variables; no state is persisted between runs.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import html
import os
import smtplib
import ssl
import sys
import uuid
from email.message import EmailMessage

import feedparser
from anthropic import Anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CENTRAL_TZ = dt.timezone(dt.timedelta(hours=-5))  # CDT approximation

MODEL = "claude-sonnet-4-6"
MAX_ITEMS_PER_FEED = 15
MAX_FEED_AGE_DAYS = 3

FEEDS: list[tuple[str, str]] = [
    ("Inside Higher Ed",        "https://www.insidehighered.com/rss.xml"),
    ("Inside Higher Ed — Tech", "https://www.insidehighered.com/news/tech-innovation/rss.xml"),
    ("EDUCAUSE Review",         "https://er.educause.edu/rss"),
    ("Chronicle of Higher Ed",  "https://www.chronicle.com/section/news/rss"),
    ("NYT Technology",          "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml"),
    ("Frontiers in Education",  "https://www.frontiersin.org/journals/education/rss"),
    ("BJET (Wiley)",            "https://onlinelibrary.wiley.com/feed/14678535/most-recent"),
]

SYSTEM_PROMPT = """You are a daily AI reading curator for a Postdoctoral Research \
Associate in AI & Emerging Technology in Higher Education at Baylor University.

Research focus:
- AI in higher education
- Teaching & learning with AI
- AI literacy
- AI frameworks
- Political / policy initiatives in AI & education

Your job: from the provided RSS headlines (and via web search for peer-reviewed \
supplements), pick:
  - 2-3 QUICK READS (5-15 min) — news, commentary, policy from Inside Higher Ed, \
    Chronicle, EDUCAUSE Review, NYT Tech, etc.
  - 1-2 PEER-REVIEWED articles (20-40 min) — from Frontiers in Education, BJET, \
    Google Scholar, or similar. Use web search to find recent ones if the RSS \
    feed did not surface any.

Quality bar: every item must plausibly matter to the user's research. Skip \
items that are only tangentially about AI or only tangentially about higher ed. \
Prefer items published in the last 3 days; never older than 2 weeks.

OUTPUT FORMAT (return ONLY this, no preamble):

## Quick Reads

1. **"[Title]"** — [Source] | [Date] | ~[X] min read
   *Why it matters:* [1-2 sentences tied to the user's research]
   [URL]

2. ...

## Peer-Reviewed

1. **"[Title]"** — [Source] | [Date] | ~[X] min read
   *Why it matters:* [1-2 sentences tied to the user's research]
   [URL]
"""


def _fmt_date(entry) -> str:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return "(undated)"
    return dt.date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday).isoformat()


def _entry_age_days(entry) -> float:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return 999.0
    published = dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - published).total_seconds() / 86400.0


def collect_headlines() -> str:
    blocks: list[str] = []
    for source, url in FEEDS:
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:
            print(f"[warn] failed to parse {source}: {exc}", file=sys.stderr)
            continue

        lines: list[str] = []
        for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
            if _entry_age_days(entry) > MAX_FEED_AGE_DAYS * 5:
                continue
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            date = _fmt_date(entry)
            summary = (entry.get("summary") or "").strip()
            if len(summary) > 280:
                summary = summary[:277] + "..."
            if not title or not link:
                continue
            lines.append(f"- [{date}] {title}\n  {link}\n  {summary}")

        if lines:
            blocks.append(f"### {source}\n" + "\n".join(lines))

    if not blocks:
        return "(no feed items retrieved)"
    return "\n\n".join(blocks)


def curate(headlines: str, today: dt.date) -> str:
    client = Anthropic()
    user_msg = (
        f"Today is {today.isoformat()}.\n\n"
        "Here are the recent RSS headlines I pulled this morning. "
        "Curate the daily reading list per your instructions. "
        "Use web_search if you need to find peer-reviewed articles "
        "not present below.\n\n"
        f"{headlines}"
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        messages=[{"role": "user", "content": user_msg}],
    )

    parts: list[str] = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip() or "(Claude returned no text)"


def markdown_to_html(md: str) -> str:
    import re
    out_lines: list[str] = []
    for raw in md.splitlines():
        line = html.escape(raw)
        if line.startswith("## "):
            out_lines.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("### "):
            out_lines.append(f"<h3>{line[4:]}</h3>")
        else:
            out_lines.append(line + "<br>")
    body = "\n".join(out_lines)
    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)
    body = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", body)
    body = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', body)
    return f"""<!doctype html>
<html><body style="font-family: -apple-system, Segoe UI, Helvetica, sans-serif;
                   max-width: 680px; line-height: 1.5; color: #222;">
{body}
</body></html>"""


def build_ics(today: dt.date, body_md: str) -> bytes:
    start = dt.datetime.combine(today, dt.time(7, 0), tzinfo=CENTRAL_TZ)
    end = start + dt.timedelta(minutes=30)
    dtstamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = f"{uuid.uuid4()}@ai-literacy.newsletter"

    def _ics_dt(d: dt.datetime) -> str:
        return d.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    desc = (
        body_md.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AI Literacy//Daily Newsletter//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{_ics_dt(start)}",
        f"DTEND:{_ics_dt(end)}",
        f"SUMMARY:\U0001f4da Daily AI Reading List \u2013 {today.isoformat()}",
        f"DESCRIPTION:{desc}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def send_email(subject: str, body_md: str, ics_bytes: bytes) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    recipient = os.environ["RECIPIENT_EMAIL"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.set_content(body_md)
    msg.add_alternative(markdown_to_html(body_md), subtype="html")
    msg.add_attachment(ics_bytes, maintype="text", subtype="calendar", filename="daily-ai-reading.ics")

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)


def main() -> int:
    today = dt.datetime.now(CENTRAL_TZ).date()
    print(f"[info] generating newsletter for {today.isoformat()}")

    headlines = collect_headlines()
    curated = curate(headlines, today)
    ics = build_ics(today, curated)
    subject = f"\U0001f4da Daily AI Reading List \u2013 {today.isoformat()}"
    send_email(subject, curated, ics)
    print(f"[info] email sent to {os.environ['RECIPIENT_EMAIL']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

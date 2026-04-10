#!/usr/bin/env python3
"""
Dual-Feed Monitor — GitHub Actions Edition
============================================
Runs every 30 minutes via GitHub Actions cron.
State (last seen post ID + news hashes) is persisted in state.json
which is committed back to the repo after each run.

Feed 1 — ntfy.sh/taco       : Trump Truth Social posts (one digest per run)
Feed 2 — ntfy.sh/private-alerts : Iran / Hormuz curated news (one digest per run)

Secrets required (set as GitHub Actions secrets):
  OPENAI_API_KEY
"""

import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests
from openai import OpenAI

try:
    from curl_cffi import requests as cffi_requests
    USE_CFFI = True
except ImportError:
    USE_CFFI = False

# ── Config ────────────────────────────────────────────────────────────────────
ACCOUNT_ID    = "107780257626128497"
TRUTH_API_URL = f"https://truthsocial.com/api/v1/accounts/{ACCOUNT_ID}/statuses"
STATE_FILE    = "state.json"   # relative — lives in repo root
NTFY_TACO     = "https://ntfy.sh/taco"
NTFY_ALERTS   = "https://ntfy.sh/private-alerts"

MAX_RSS_CANDIDATES = 15
DEDUP_WINDOW_HOURS = 24

RSS_FEEDS = [
    "https://news.google.com/rss/search?q=iran+hormuz+strait&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=iran+nuclear+oil+military&hl=en-US&gl=US&ceid=US:en",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
]

IRAN_KEYWORDS = [
    "iran", "hormuz", "strait of hormuz", "tehran", "khamenei",
    "irgc", "nuclear deal", "uranium enrichment", "tanker",
    "persian gulf", "iran nuclear", "iran oil", "iran attack",
    "iran missile", "pezeshkian",
]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)
ai = OpenAI()   # reads OPENAI_API_KEY from env


# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"State load error: {e}")
    return {"last_seen_truth_id": None, "sent_news_hashes": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def prune_hashes(hashes):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=DEDUP_WINDOW_HOURS)).isoformat()
    return [h for h in hashes if h.get("ts", "") >= cutoff]


def make_hash(text):
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]


# ── HTML strip ────────────────────────────────────────────────────────────────
def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?p>", "\n", text, flags=re.I)
    text = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>.*?</a>', r'\1', text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "", text)
    for e, c in [("&amp;","&"),("&lt;","<"),("&gt;",">"),("&quot;",'"'),("&#39;","'"),("&apos;","'")]:
        text = text.replace(e, c)
    return text.strip()


# ── ntfy ──────────────────────────────────────────────────────────────────────
def send_ntfy(url, title, body, priority="default", tags=""):
    headers = {"Title": title.encode("utf-8"), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=10)
        log.info(f"ntfy {'OK' if r.status_code==200 else r.status_code} → {url}")
    except Exception as e:
        log.error(f"ntfy error: {e}")


# ── GPT: single batch call per feed ──────────────────────────────────────────
def gpt_digest_trump(raw_posts):
    numbered = "\n\n".join(f"{i+1}. {p[:400]}" for i, p in enumerate(raw_posts))
    try:
        resp = ai.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content":
                    "Summarize each numbered Trump Truth Social post into a single neutral one-liner "
                    "(max 110 chars each). Return ONLY a numbered list matching the input order. "
                    "No quotes, no hashtags, no extra text."},
                {"role": "user", "content": numbered},
            ],
            max_tokens=300,
            temperature=0.3,
        )
        lines = resp.choices[0].message.content.strip().splitlines()
        results = [re.sub(r'^\d+[\.\)]\s*', '', l).strip() for l in lines]
        return [r for r in results if r]
    except Exception as e:
        log.warning(f"GPT trump digest failed: {e}")
        return [p[:110] for p in raw_posts]


def gpt_digest_news(headlines):
    numbered = "\n\n".join(
        f"{i+1}. {t} — {d[:200]}" for i, (t, d) in enumerate(headlines)
    )
    try:
        resp = ai.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content":
                    "You are a strict Iran/Hormuz news curator. "
                    "From the numbered headlines below, select ONLY those directly about "
                    "Iran, the Strait of Hormuz, or Iranian nuclear/oil/military affairs. "
                    "For each selected item write a single neutral one-liner (max 110 chars). "
                    "Return ONLY the selected one-liners as a plain bullet list (• prefix). "
                    "If none are relevant, return the single word: NONE."},
                {"role": "user", "content": numbered},
            ],
            max_tokens=400,
            temperature=0.1,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.upper() == "NONE" or not raw:
            return []
        lines = [re.sub(r'^[•\-\*]\s*', '', l).strip() for l in raw.splitlines() if l.strip()]
        return [l for l in lines if l and l.upper() != "NONE"]
    except Exception as e:
        log.warning(f"GPT news digest failed: {e}")
        return []


# ── Truth Social ──────────────────────────────────────────────────────────────
def process_truth_posts(state):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://truthsocial.com/",
    }
    try:
        if USE_CFFI:
            resp = cffi_requests.get(TRUTH_API_URL,
                                     params={"limit": 10, "exclude_replies": "false",
                                             "exclude_reblogs": "false"},
                                     headers=headers, impersonate="chrome110", timeout=20)
        else:
            resp = requests.get(TRUTH_API_URL,
                                params={"limit": 10, "exclude_replies": "false",
                                        "exclude_reblogs": "false"},
                                headers=headers, timeout=20)
        resp.raise_for_status()
        posts = resp.json()
    except Exception as e:
        log.error(f"Truth Social fetch failed: {e}")
        return

    if not isinstance(posts, list) or not posts:
        return

    last_seen = state.get("last_seen_truth_id")
    new_posts = [p for p in posts if not last_seen or p.get("id", "") > last_seen]
    log.info(f"Truth Social: {len(new_posts)} new post(s)")

    if not new_posts:
        return

    raw_texts = []
    for post in reversed(new_posts):
        reblog = post.get("reblog")
        if reblog:
            raw = strip_html(reblog.get("content", ""))
            author = reblog.get("account", {}).get("display_name", "someone")
            raw = f"[ReTruth of {author}] {raw}"
        else:
            raw = strip_html(post.get("content", ""))
        if not raw.strip() and post.get("media_attachments"):
            raw = "[Image post]"
        if raw.strip():
            raw_texts.append(raw)

    if raw_texts:
        summaries = gpt_digest_trump(raw_texts)
        body = "\n".join(f"• {s}" for s in summaries)
        count = len(summaries)
        send_ntfy(NTFY_TACO,
                  f"Trump — {count} new post{'s' if count > 1 else ''}",
                  body, priority="default", tags="loudspeaker")

    state["last_seen_truth_id"] = posts[0].get("id")


# ── News feeds ────────────────────────────────────────────────────────────────
def fetch_rss_items(feed_url):
    try:
        resp = requests.get(feed_url, timeout=10,
                            headers={"User-Agent": "Mozilla/5.0 NewsMonitor/1.0"})
        resp.raise_for_status()
        content = resp.text
        items = []
        entries = re.findall(r'<item>(.*?)</item>', content, re.S) or \
                  re.findall(r'<entry>(.*?)</entry>', content, re.S)
        for entry in entries:
            title = re.search(r'<title[^>]*>(.*?)</title>', entry, re.S)
            desc  = re.search(r'<description[^>]*>(.*?)</description>', entry, re.S) or \
                    re.search(r'<summary[^>]*>(.*?)</summary>', entry, re.S)
            t = strip_html(title.group(1)) if title else ""
            d = strip_html(desc.group(1))  if desc  else ""
            if t:
                items.append((t, d))
        return items
    except Exception as e:
        log.warning(f"RSS error {feed_url}: {e}")
        return []


def process_news_feeds(state):
    sent_hashes = prune_hashes(state.get("sent_news_hashes", []))
    seen = {h["hash"] for h in sent_hashes}
    candidates = []

    for feed_url in RSS_FEEDS:
        if len(candidates) >= MAX_RSS_CANDIDATES:
            break
        items = fetch_rss_items(feed_url)
        log.info(f"RSS: {len(items)} items from {feed_url.split('?')[0][-40:]}")
        for title, desc in items:
            if len(candidates) >= MAX_RSS_CANDIDATES:
                break
            combined = (title + " " + desc).lower()
            if not any(kw in combined for kw in IRAN_KEYWORDS):
                continue
            h = make_hash(title)
            if h in seen:
                continue
            candidates.append((title, desc))
            seen.add(h)

    log.info(f"News candidates: {len(candidates)}")

    now = datetime.now(timezone.utc).isoformat()
    for title, _ in candidates:
        sent_hashes.append({"hash": make_hash(title), "ts": now})

    if candidates:
        one_liners = gpt_digest_news(candidates)
        log.info(f"News one-liners after curation: {len(one_liners)}")
        if one_liners:
            body = "\n".join(f"• {l}" for l in one_liners)
            count = len(one_liners)
            send_ntfy(NTFY_ALERTS,
                      f"Iran/Hormuz — {count} update{'s' if count > 1 else ''}",
                      body, priority="high", tags="iran,ship")

    state["sent_news_hashes"] = sent_hashes


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=== Monitor starting ===")
    state = load_state()
    process_truth_posts(state)
    process_news_feeds(state)
    save_state(state)
    log.info("=== Monitor done ===")


if __name__ == "__main__":
    main()

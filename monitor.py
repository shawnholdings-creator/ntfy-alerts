import json
import logging
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List
from urllib.parse import quote_plus, urlparse

import feedparser
import requests

# =========================
NTFY_TOPIC = "oilmacro"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
STATE_FILE = Path("state.json")

SEARCH_QUERIES = [
    "Iran Strait of Hormuz oil",
    "Iran tanker sanctions oil",
    "Middle East oil shipping disruption",
    "OPEC oil supply Iran",
]

# --- Direct RSS feeds (free, no API key) ---
DIRECT_RSS_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",           # BBC World
    "https://feeds.bbci.co.uk/news/business/rss.xml",         # BBC Business
    "https://www.aljazeera.com/xml/rss/all.xml",               # Al Jazeera
    "https://www.cnbc.com/id/100727362/device/rss/rss.html",   # CNBC Business
    "https://www.cnbc.com/id/19854910/device/rss/rss.html",    # CNBC Energy
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",  # MarketWatch Top
    "https://feeds.content.dowjones.io/public/rss/mw_bulletins",   # MarketWatch Bulletins
    "https://news.yahoo.com/rss/world",                        # Yahoo News World
    "https://www.axios.com/feeds/feed.rss",                    # Axios
]

KEYWORDS = [
    "iran","strait of hormuz","hormuz","oil","crude","brent","opec",
    "tanker","shipping","sanctions","middle east","energy",
    "xom","cvx","oxy","fang","slb","hal",
]

HIGH_PRIORITY = [
    "strait of hormuz","hormuz","iran","tanker","oil supply",
    "shipping disruption","sanctions","missile","attack",
    "closure","blockade",
]

PREFERRED_SOURCES = [
    "reuters.com","cnbc.com","bloomberg.com","ft.com",
    "wsj.com","apnews.com","barrons.com","fortune.com",
    "bbc.com","bbc.co.uk",
    "aljazeera.com",
    "axios.com",
    "marketwatch.com",
    "yahoo.com","finance.yahoo.com",
]

BLOCKED_SOURCES = [
    "dailymail.co.uk","laodong.vn",
]

MAX_ALERTS_PER_RUN = 4
REQUEST_TIMEOUT = 20
SIMILARITY_THRESHOLD = 0.75   # titles >75% similar = duplicate

# --- COOLDOWN GUARD ---
# Maximum alerts allowed within the cooldown window.
# Prevents notification floods if state.json resets.
COOLDOWN_MAX_ALERTS = 4
COOLDOWN_WINDOW_SECONDS = 2 * 60 * 60  # 2 hours

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =========================
def load_state() -> Dict:
    if not STATE_FILE.exists():
        return {"sent_links": [], "sent_titles": [], "send_timestamps": []}
    try:
        data = json.loads(STATE_FILE.read_text())
        data.setdefault("sent_links", [])
        data.setdefault("sent_titles", [])
        data.setdefault("send_timestamps", [])
        return data
    except Exception as e:
        logging.warning(f"Corrupted state.json, resetting: {e}")
        return {"sent_links": [], "sent_titles": [], "send_timestamps": []}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

# =========================
def cooldown_remaining(state) -> int:
    """Returns how many alerts we can still send within the cooldown window."""
    now = time.time()
    cutoff = now - COOLDOWN_WINDOW_SECONDS
    # Prune old timestamps
    recent = [ts for ts in state.get("send_timestamps", []) if ts > cutoff]
    state["send_timestamps"] = recent
    return max(0, COOLDOWN_MAX_ALERTS - len(recent))

# =========================
def rss_url(q):
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"

def normalize_title(t):
    """Strip source suffix, punctuation, stop words → canonical form."""
    t = t.lower()
    t = re.sub(r"\s*-\s*[^-]+$", "", t)          # remove "- Source Name"
    t = re.sub(r"\s*\|.*$", "", t)                 # remove "| Source Name"
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\b(the|a|an|and|or|to|of|in|for|on|as|with|is|are|was|were|has|have|that|this|will|from|by)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def title_fingerprint(t):
    """Extract core keywords sorted alphabetically for fuzzy matching."""
    words = set(normalize_title(t).split())
    # Remove very short words
    words = {w for w in words if len(w) > 2}
    return " ".join(sorted(words))

def is_similar(title_a, existing_titles, threshold=SIMILARITY_THRESHOLD):
    """Check if title_a is too similar to any previously sent title."""
    fp_a = title_fingerprint(title_a)
    for existing in existing_titles:
        fp_b = title_fingerprint(existing)
        if SequenceMatcher(None, fp_a, fp_b).ratio() >= threshold:
            return True
    return False

def domain(link):
    try:
        return urlparse(link).netloc.replace("www.","")
    except Exception:
        return ""

def score(text, dom):
    s = 0
    for p in HIGH_PRIORITY:
        if p in text: s += 2
    for k in KEYWORDS:
        if k in text: s += 1
    if any(p in dom for p in PREFERRED_SOURCES): s += 4
    if any(b in dom for b in BLOCKED_SOURCES): s -= 5
    return s

# =========================
def fetch():
    out = []

    # --- Google News RSS (aggregated search results) ---
    for q in SEARCH_QUERIES:
        logging.info(f"Fetching Google News: {q}")
        feed = feedparser.parse(rss_url(q))
        for e in feed.entries:
            _process_entry(e, out)

    # --- Direct RSS feeds ---
    for url in DIRECT_RSS_FEEDS:
        logging.info(f"Fetching direct feed: {url}")
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                _process_entry(e, out)
        except Exception as e:
            logging.warning(f"Failed to fetch {url}: {e}")

    return dedupe(out)

def _process_entry(e, out):
    """Filter and score a single RSS entry, append to out if relevant."""
    title = e.get("title","")
    link = e.get("link","")
    text = title.lower()
    dom = domain(link)
    nt = normalize_title(title)

    if not link or not any(k in text for k in KEYWORDS):
        return
    if any(b in dom for b in BLOCKED_SOURCES):
        return

    out.append({
        "title": title,
        "nt": nt,
        "link": link,
        "dom": dom,
        "score": score(text, dom)
    })

def dedupe(items):
    """Remove duplicates by link, normalized title, AND fuzzy similarity."""
    seen_t = set()
    seen_l = set()
    seen_fp = []    # fingerprints for fuzzy matching
    res = []

    for i in sorted(items, key=lambda x: x["score"], reverse=True):
        if i["link"] in seen_l or i["nt"] in seen_t:
            continue

        # Fuzzy check: skip if >75% similar to any already-accepted item
        fp = title_fingerprint(i["title"])
        if any(SequenceMatcher(None, fp, s).ratio() >= SIMILARITY_THRESHOLD for s in seen_fp):
            logging.info(f"Fuzzy-duped: {i['title']}")
            continue

        seen_l.add(i["link"])
        seen_t.add(i["nt"])
        seen_fp.append(fp)
        res.append(i)

    return res

# =========================
def send(i):
    body = f"{i['title']}\n📰 {i['dom']}"
    headers = {
        "Title": "Oil Macro Alert",
        "Priority": "high" if i["score"] >= 8 else "default",
        "Click": i["link"],
    }
    requests.post(NTFY_URL, data=body.encode(), headers=headers, timeout=REQUEST_TIMEOUT).raise_for_status()

# =========================
def main():
    logging.info("=== Monitor start ===")

    state = load_state()
    sent_l = set(state["sent_links"])
    sent_t = set(state["sent_titles"])

    # --- COOLDOWN CHECK ---
    budget = cooldown_remaining(state)
    if budget <= 0:
        logging.info("Cooldown active — skipping this run to prevent flood.")
        logging.info("=== Done (cooldown) ===")
        return

    items = fetch()
    sent = 0

    for i in items:
        if sent >= min(MAX_ALERTS_PER_RUN, budget):
            break

        # Exact dedup: link or normalized title already sent
        if i["link"] in sent_l or i["nt"] in sent_t:
            continue

        # Fuzzy dedup: check against all previously sent titles
        if is_similar(i["title"], sent_t):
            logging.info(f"Fuzzy-skipped (already sent): {i['title']}")
            continue

        try:
            send(i)
            sent_l.add(i["link"])
            sent_t.add(i["nt"])
            state["send_timestamps"].append(time.time())
            sent += 1
            logging.info(f"Sent: {i['title']}")
        except Exception as e:
            logging.error(e)

    state["sent_links"] = list(sent_l)[-1000:]
    state["sent_titles"] = list(sent_t)[-1000:]
    save_state(state)

    logging.info(f"Sent alerts: {sent}")
    logging.info("=== Done ===")

if __name__ == "__main__":
    main()

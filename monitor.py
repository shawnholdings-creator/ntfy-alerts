import json
import logging
from pathlib import Path
from typing import Dict, List
from urllib.parse import quote_plus

import feedparser
import requests

# =========================
# Config
# =========================
NTFY_TOPIC = "oilmacro-shawn-9f27x-alerts"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
STATE_FILE = Path("state.json")

SEARCH_QUERIES = [
    "Iran Strait of Hormuz oil",
    "Iran tanker sanctions oil",
    "Middle East oil shipping disruption",
    "OPEC oil supply Iran",
]

KEYWORDS = [
    "iran",
    "strait of hormuz",
    "hormuz",
    "oil",
    "crude",
    "brent",
    "opec",
    "tanker",
    "shipping",
    "sanctions",
    "middle east",
    "energy",
    "xom",
    "cvx",
    "oxy",
    "fang",
    "slb",
    "hal",
]

HIGH_PRIORITY = [
    "strait of hormuz",
    "hormuz",
    "iran",
    "tanker",
    "oil supply",
    "shipping disruption",
    "sanctions",
    "missile",
    "attack",
    "closure",
    "blockade",
]

MAX_ALERTS_PER_RUN = 8
REQUEST_TIMEOUT = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# =========================
# State
# =========================
def load_state() -> Dict[str, List[str]]:
    if not STATE_FILE.exists():
        return {"sent_links": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {"sent_links": data}
        if isinstance(data, dict) and "sent_links" in data:
            return data
    except Exception as exc:
        logging.warning("Failed to read state.json: %s", exc)
    return {"sent_links": []}


def save_state(state: Dict[str, List[str]]) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

# =========================
# Feed + filter
# =========================
def google_news_rss_url(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"


def normalize_text(*parts: str) -> str:
    return " ".join((p or "").strip() for p in parts if p).lower()


def matches_keywords(text: str) -> bool:
    return any(k in text for k in KEYWORDS)


def priority_score(text: str) -> int:
    score = 0
    for phrase in HIGH_PRIORITY:
        if phrase in text:
            score += 2
    for k in KEYWORDS:
        if k in text:
            score += 1
    return score


def fetch_google_news() -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []

    for query in SEARCH_QUERIES:
        url = google_news_rss_url(query)
        parsed = feedparser.parse(url)

        logging.info("RSS: %s items from %s", len(parsed.entries), url)

        for entry in parsed.entries:
            title = getattr(entry, "title", "") or ""
            summary = getattr(entry, "summary", "") or ""
            link = getattr(entry, "link", "") or ""

            text = normalize_text(title, summary)

            if not link or not matches_keywords(text):
                continue

            items.append(
                {
                    "title": title.strip(),
                    "link": link.strip(),
                    "text": text,
                    "score": priority_score(text),
                }
            )

    return deduplicate_and_sort(items)


def deduplicate_and_sort(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    out: List[Dict[str, str]] = []

    for item in items:
        if item["link"] in seen:
            continue
        seen.add(item["link"])
        out.append(item)

    out.sort(key=lambda x: x["score"], reverse=True)
    return out

# =========================
# Notify
# =========================
def send_ntfy(item: Dict[str, str]) -> None:
    body = f"{item['title']}\nScore: {item['score']}\n{item['link']}"

    headers = {
        "Title": "Oil Macro Alert",
        "Priority": "high" if item["score"] >= 4 else "default",
        "Tags": "oil,news",
    }

    r = requests.post(
        NTFY_URL,
        data=body.encode("utf-8"),
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()

# =========================
# Main
# =========================
def main():
    logging.info("=== Monitor starting ===")

    state = load_state()
    sent_links = set(state.get("sent_links", []))

    candidates = fetch_google_news()
    logging.info("News candidates: %s", len(candidates))

    sent = 0
    updated = list(sent_links)

    for item in candidates:
        if item["link"] in sent_links:
            continue

        try:
            send_ntfy(item)
            updated.append(item["link"])
            sent += 1
            logging.info("Sent alert: %s", item["title"])
        except Exception as e:
            logging.error("Send failed: %s", e)

        if sent >= MAX_ALERTS_PER_RUN:
            break

    state["sent_links"] = updated[-1000:]
    save_state(state)

    logging.info("Sent alerts: %s", sent)
    logging.info("=== Monitor done ===")


if __name__ == "__main__":
    main()

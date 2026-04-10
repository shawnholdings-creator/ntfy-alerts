# ntfy-alerts

Automated monitor that runs every 30 minutes via GitHub Actions.

## Feeds

| Feed | Content |
|---|---|
| `ntfy.sh/taco` | Trump Truth Social posts — one digest per run |
| `ntfy.sh/private-alerts` | Iran / Strait of Hormuz curated news — one digest per run |

## How it works

1. Fetches Trump's latest Truth Social posts via the public API
2. Summarizes new posts into one-liners using GPT-4.1-mini
3. Scrapes Iran/Hormuz RSS feeds, deduplicates by hash, curates with GPT
4. Sends a single batched notification per feed (no spam)
5. Commits updated `state.json` back to the repo for deduplication persistence

## Secrets required

Set these in **Settings → Secrets and variables → Actions**:

- `OPENAI_API_KEY` — OpenAI API key for GPT summarization
- `GH_PAT` — GitHub fine-grained PAT with Contents + Actions read/write

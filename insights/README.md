# insights

Weekly reports on what's working. Both only read the platforms' own files (through `outcomes.py`) and, for
`sources.py`, Reddit's public JSON. Neither changes a prompt or a config: they suggest, you decide.
`daily.py` runs both on Mondays.

    venv/bin/python insights/run.py        # reply/booking rates by platform, score, category, source; prompt edits
    venv/bin/python insights/sources.py    # where to look for leads (below)

## Source suggestions (`sources.py`)

    venv/bin/python insights/sources.py                 # full run: writes insights/reports/sources-<date>.md
    venv/bin/python insights/sources.py --no-search     # offline: no Reddit requests
    venv/bin/python insights/sources.py --no-ai         # no Claude recommendation
    venv/bin/python insights/sources.py --min 10 --max-requests 24

The report:

- **Recommendation** (top, with `ANTHROPIC_API_KEY` in the environment or any platform's `.env`): a short
  ranked add / drop / watch list from Claude, citing the numbers and saying when a sample is too small.
- **Performance**: for each subreddit, Reddit keyword, Facebook keyword and group, and Instagram account and
  post: found, qualified (at or above that platform's `ai.min_score`), DMed, replied, booked. Flags sources
  with leads but no replies over `--min` DMs, ones that find nothing, and small samples.
- **New subreddit candidates**: Reddit's subreddit search for a few topics built from `business.services`
  and the current keywords, then a `/r/<sub>/new.json` sample of the likeliest ones (posts per day, share
  matched by the current keywords). Already-configured, NSFW, quarantined, private, small (<2,000) and quiet
  (<1 post/day) subreddits are left out. It uses the logged-in Reddit profile and `reddit/browser.py`'s
  `fetch_json`, spaced like the scraper, at most `--max-requests` requests. It only reads, and it stops at once
  on anything account-wide (logged out, rate-limited, blocked), writes the report with what it has and exits 2,
  so `daily.py` notifies you. Don't run it while another Reddit script has the profile open.
- **New keyword candidates**: phrases common in posts that got replies or scored 80+, but no current keyword
  covers, for Reddit and Facebook.
- **Facebook groups**: Facebook is never searched for this. Phrases to type into Facebook's group search,
  groups that keyword searches already found leads in, and the configured groups by performance.
- **Config snippets**: ready-to-paste JSON for the suggested changes (the whole list at each key). Nothing is
  applied.

Tests (offline, fake Reddit, mocked Claude): `venv/bin/python -m unittest discover -s insights/tests`

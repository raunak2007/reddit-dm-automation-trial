# Facebook Lead Finder

Finds recent Facebook posts from students and parents who need help with college admissions,
essays, scholarships, financial aid or internships. Claude scores each post and drafts a
personalized message pointing them to your booking link. You review each one in a local queue
and send it yourself.

```
scrape.py ──► data/leads.json ──► qualify.py (Claude) ──► queue_server.py ──► you send in Messenger
 (your logged-in            (score, flags,            (review, edit,         data/contacted.csv
  Facebook session)          DM + comment drafts)      copy & open chat)      (who's been contacted)
```

**Nothing here sends a message automatically.** Facebook restricts accounts that send automated
messages to people they aren't friends with, and many people posting about admissions are under
18. So the scripts find, score and draft, and you press send. That takes about 10 seconds per lead.

## Setup

Uses the same Python 3.11 setup as the Reddit scripts on `master`.

```bash
cd facebook
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your ANTHROPIC_API_KEY in it
python run.py login         # opens Chrome: log in to Facebook once; the session is saved
```

To use your everyday Chrome profile instead of a separate one, set `browser.user_data_dir` in
`config.json` (on a Mac: `~/Library/Application Support/Google/Chrome`) and quit Chrome before
running.

## Make it yours

1. **`config.json` → `business`**: your company name, website, booking link, your first name,
   your services, and the offer (for example, a free 20-minute call).
2. **`config.json` → `search`**:
   - `keywords`: the phrases to search for.
   - `groups`: URLs of Facebook groups you've joined (college parents groups, class-of-2027
     groups, scholarship groups…). Groups are usually where the best leads are.
   - `lookback_days`: defaults to 14.
3. **`prompts/qualify.md`**: who counts as a lead, and how to score them.
4. **`prompts/message.md`**: the tone, length and structure of the DM and the public comment.
   This is the file to edit when you want to change how the messages read.

Placeholders like `{booking_link}` and `{services}` are filled in from `config.json`. To check the
exact prompt Claude will see, run `python run.py qualify --dry-run`. After you change a prompt,
run `python run.py qualify --rescore` to redraft every lead.

## Daily use

```bash
python run.py                      # scrape -> score/draft -> open the review queue
python run.py scrape --limit-pages 1   # quick test on one search page
python run.py queue                # reopen the queue without scraping
python run.py stats
```

In the queue (http://127.0.0.1:8765):

| Button | What it does |
|---|---|
| **Copy & open Messenger** | Copies your edited draft and opens a chat with the author. Paste it, read it once more, send. |
| **Mark sent** | Records the DM. That person is never offered again, even from other posts. |
| **Copy comment & open post** / **Mark commented** | Use a helpful public reply instead of a DM. |
| **Skip** / **Not relevant** | Hides the post. "Not relevant" also keeps a record of where the AI was wrong. |

Edits to drafts save automatically. Leads are sorted by score.

## Guardrails

- **Daily DM cap** (`outreach.daily_dm_limit`, default 20). The queue server enforces it, counting
  across every run. New or low-activity accounts should start around 10.
- **One DM per person, ever.**
- **Minors** (`outreach.minor_policy`): if the AI thinks the author is under 18, the default
  `comment_only` turns off the DM and offers only the public comment. Set it to `skip` to hide
  those leads. A parent posting about their child is not flagged.
- **The scraper stops** at any checkpoint, "temporarily blocked" notice or logout, and keeps what
  it already found. If that happens, wait a day before running it again.
- It moves at human speed: random 4–9 second waits between pages and short scroll pauses.

## Notes

- Facebook changes its page markup often. If a run finds 0 posts, look at the open window and
  update the selectors in `_EXTRACT` in `scrape.py`.
- Some authors' profile links can't be read (some search layouts hide them). Those leads still
  show up, with a note to message the person from the post.
- Timestamps that Facebook scrambles are kept as "date unknown" when `keep_undated_posts` is
  `true`. Set it to `false` to drop them.
- Facebook's terms don't allow automated collection, so keep the number of pages read per day
  modest. The scraper uses your account, and the risk falls on your account.

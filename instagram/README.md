# Instagram Commenter Outreach

Give it the link to an Instagram post or reel. It reads everyone who commented, has Claude pick out
the ones asking about college admissions, essays, scholarships or internships, and writes each of
them a short, casual DM that starts with their name. You then follow and DM each person from a review
page.

```bash
python run.py https://www.instagram.com/p/XXXXXXXX/
```

**It never follows or sends by itself.** Instagram restricts accounts quickly for automated follows
and DMs, and many commenters on admissions posts are teenagers. So you send each one yourself from
the review page. That's three clicks per person.

## Setup (once)

```bash
cd instagram
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add your ANTHROPIC_API_KEY
python run.py login           # log in to Instagram in the Chrome window that opens
```

In `config.json`, set `instagram.my_username` to your handle so you're never in your own list, and
fill in `business.website` and `business.booking_link`.

## Write your message

`message_template` in `config.json` is your message:

```
hey {first_name}! saw your comment about {topic} - we're Aristo Scholars, we help students with
college apps, essays and scholarships. happy to hop on a free call and give you some pointers if
that'd help: {booking_link}
```

- `{first_name}`: their first name, taken from their display name or an obvious username. When
  there's no clear name, the message starts with "hey!".
- `{topic}`: what *their* comment was actually about ("the essay stuff", "finding scholarships"),
  so "saw your comment about…" is always true.
- `{booking_link}`, `{website}` and `{business_name}` are filled in from `config.json`.

The style rules (casual, lowercase-friendly, short, no corporate wording) are in
`prompts/message.md`. Edit them freely. `python run.py preview` prints exactly what Claude gets, and
`python run.py redraft` rewrites every unsent DM after you change something.

With `"ai": {"enabled": false}`, every commenter gets the template as is, with `{topic}` = "college
stuff". There's no filtering then, so "congrats!" and emoji-only comments show up too, and nothing
checks for minors.

## The review page

It opens at http://127.0.0.1:8766. Each commenter shows their comment, a score and the draft DM,
which you can edit.

1. **Open profile (follow)**: opens their profile so you can click Follow.
2. **Copy DM & open chat**: copies the DM and opens a chat with them. Paste it and send.
3. **Mark sent**: records the DM. That person is never offered again, from any post.

**Skip** hides them for this post. **Not a lead** hides them and records that the AI got it wrong.

## Limits it enforces

- `outreach.daily_dm_limit` (default 20), counted across all runs. Instagram puts DMs to people who
  don't follow you into Message Requests, and a burst of them is what triggers action blocks. Stay
  around 20–30 a day, fewer on a new account.
- One DM per person, ever.
- Commenters who seem to be under 18 are hidden (`outreach.minor_policy: "skip"`).
- Comment reading stops at once if Instagram rate-limits or logs out the profile.

## Files

| File | What it does |
|---|---|
| `run.py` | The commands above. |
| `comments.py` | Reads all comments and replies, using Instagram's own web endpoint inside your logged-in session. Falls back to reading the page. |
| `qualify.py` | Scores each comment with Claude and writes the DM. |
| `queue_server.py`, `queue.html` | The review page. |
| `data/` | `leads.json` (commenters and drafts) and `contacted.csv` (who you've DMed). Not committed. |

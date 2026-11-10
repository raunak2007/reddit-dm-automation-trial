"""qualify.py - score each scraped Reddit post with Claude and draft a subject and private message for it.

The instructions come from two files you edit:
    prompts/qualify.md   who counts as a lead and how to score them
    prompts/message.md   how the message and subject should read
{placeholders} in them are filled from config.json's business block: {business_name}, {website},
{booking_link}, {sender_first_name}, {offer}, {services}. Write a literal brace as {{ or }}.

Only posts without an "ai" block are sent to Claude, so re-running is cheap; posts by anyone the root
contacted.csv says not to message (already sent, held, given up on) are left out. --rescore re-does all of them
(after you change a prompt), --dry-run prints the filled prompt and the first post and stops.

    python qualify.py [--rescore] [--dry-run]

Needs ANTHROPIC_API_KEY in the environment or in reddit/.env.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import store  # first: it puts the repo root on sys.path
import ledger  # noqa: E402

HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"

TOOL = {
    "name": "record_lead",
    "description": "Record the assessment of one Reddit post and, if relevant, the drafted private message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean", "description": "Would this author plausibly welcome our help?"},
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "categories": {"type": "array", "items": {"type": "string", "enum": [
                "counselor", "admissions_anxiety", "essays", "school_list", "scholarships", "financial_aid",
                "internships", "summer_programs", "resume", "transfer", "grad_school", "other"]}},
            "author_role": {"type": "string", "enum": ["student", "parent", "other", "unclear"]},
            "likely_minor": {"type": "boolean"},
            "minor_evidence": {"type": "string", "description": "Why you think the author is or isn't under 18"},
            "need": {"type": "string", "description": "One sentence: what they need help with"},
            "reason": {"type": "string", "description": "One sentence: why this score"},
            "subject": {"type": "string", "description": "The message's subject line; empty if not relevant"},
            "message": {"type": "string", "description": "The private message; empty if not relevant"},
        },
        "required": ["relevant", "score", "categories", "author_role", "likely_minor", "minor_evidence", "need",
                     "reason", "subject", "message"],
    },
}


def make_client():
    """The Anthropic client, with ANTHROPIC_API_KEY from the environment or reddit/.env (followup.py uses it too)."""
    import anthropic
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
    return anthropic.Anthropic()


def business_values(cfg: dict) -> dict:
    """The {placeholders} the prompts (and followup.template) may use, from config.json's business block."""
    b = cfg["business"]
    return {"business_name": b["name"], "website": b["website"], "booking_link": b["booking_link"],
            "sender_first_name": b["sender_first_name"], "offer": b["offer"],
            "services": "\n".join(f"- {s}" for s in b["services"])}


def system_prompt(cfg: dict) -> str:
    values = business_values(cfg)
    parts = [(PROMPTS / name).read_text().format(**values) for name in ("qualify.md", "message.md")]
    return ("\n\n".join(parts) + f"\n\nToday is {datetime.now():%B %d, %Y}. Only posts from the last "
            f"{cfg['search']['lookback_days']} days count. Always answer by calling record_lead.")


def post_message(lead: dict) -> str:
    return (f"Username: {lead.get('author') or 'unknown'}\n"
            f"Subreddit: r/{lead.get('subreddit') or 'unknown'}\n"
            f"Flair: {lead.get('flair') or 'none'}\n"
            f"Posted: {lead.get('created_at') or 'unknown'}\n"
            f"Found via: {', '.join(lead.get('sources', []))}\n\n"
            f"Title: {lead.get('title', '')}\n\n"
            f"Post:\n\"\"\"\n{(lead.get('text') or '(no text, title only)')[:4000]}\n\"\"\"")


def assess(client, cfg: dict, system: str, lead: dict) -> dict:
    resp = client.messages.create(
        model=cfg["ai"]["model"],
        max_tokens=1200,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "record_lead"},
        messages=[{"role": "user", "content": post_message(lead)}],
    )
    block = next(b for b in resp.content if b.type == "tool_use")
    return dict(block.input, model=cfg["ai"]["model"], scored_at=datetime.now().isoformat(timespec="seconds"))


def to_score(leads: dict, cfg: dict, rescore=False) -> list[dict]:
    """The leads to send to Claude this run, newest first, up to ai.max_posts_per_run."""
    max_attempts = store.load_root_config().get("max_attempts_per_recipient", ledger.MAX_ATTEMPTS)
    todo = [l for l in leads.values()
            if (rescore or "ai" not in l) and not ledger.skip_reason(l["author"], max_attempts)]
    todo.sort(key=lambda l: -float(l.get("created_utc") or 0))
    return todo[:cfg["ai"]["max_posts_per_run"]]


def run(cfg: dict, rescore=False, dry_run=False, client=None) -> int:
    leads = store.load_leads()
    todo = to_score(leads, cfg, rescore)
    system = system_prompt(cfg)
    if dry_run:
        print(system, "\n\n----- first post -----\n", post_message(todo[0]) if todo else "(no posts to score)")
        return 0
    if not todo:
        print("[+] Nothing new to score.")
        return 0

    import anthropic
    client = client or make_client()
    hits = 0
    print(f"[+] Scoring {len(todo)} posts with {cfg['ai']['model']}...")
    try:
        for i, lead in enumerate(todo, 1):
            try:
                lead["ai"] = assess(client, cfg, system, lead)
            except anthropic.AuthenticationError:
                sys.exit("ANTHROPIC_API_KEY is missing or invalid (set it in reddit/.env).")
            except anthropic.APIError as exc:
                print(f"[!] {lead['post_id']}: {exc}; will retry next run")
                continue
            a = lead["ai"]
            good = a["relevant"] and a["score"] >= cfg["ai"]["min_score"]
            hits += good
            flag = " [minor]" if a["likely_minor"] else ""
            print(f"[{i}/{len(todo)}] {a['score']:>3} {'LEAD' if good else '    '}{flag} u/{lead['author']} "
                  f"r/{lead.get('subreddit', '?')}: {a['need'][:90]}")
            if i % 10 == 0:
                store.save_leads(leads)
    finally:
        store.save_leads(leads)
    print(f"[done] {hits} leads at score >= {cfg['ai']['min_score']}")
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(store.load_config(), args.rescore, args.dry_run)


if __name__ == "__main__":
    main()

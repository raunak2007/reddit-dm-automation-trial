"""qualify.py - decide which commenters are leads and write each one's DM.

With ai.enabled (the default), Claude reads each comment, scores it, flags commenters who seem to be under
18, and writes the DM from config.json's message_template following prompts/message.md: their first name,
what their comment was about, casual wording.

With ai.enabled false, every commenter is a lead and gets message_template as is, with {first_name} from
their display name (or "hey!" without a name) and {topic} as "college stuff". Nothing filters out the
"congrats!" and emoji comments then, so read the queue carefully.

    python qualify.py [--rescore] [--dry-run]

Placeholders in message_template and prompts/message.md: {first_name}, {topic}, {business_name}, {website},
{booking_link}, {sender_first_name}, {offer}, {services}. Needs ANTHROPIC_API_KEY (in instagram/.env) for AI.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import store

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
PROMPT = HERE / "prompts" / "message.md"

TOOL = {
    "name": "record_commenter",
    "description": "Record the assessment of one Instagram commenter and the DM for them.",
    "input_schema": {
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean"},
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "need": {"type": "string", "description": "A few words: what they want help with, or 'none'"},
            "likely_minor": {"type": "boolean"},
            "minor_evidence": {"type": "string"},
            "first_name": {"type": "string", "description": "Their first name if obvious, else empty"},
            "dm": {"type": "string", "description": "The DM; empty if not relevant"},
        },
        "required": ["relevant", "score", "need", "likely_minor", "minor_evidence", "first_name", "dm"],
    },
}


class _Keep(dict):
    def __missing__(self, k):                 # leave {first_name} / {topic} for the per-person fill
        return "{" + k + "}"


def values(cfg: dict) -> dict:
    b = cfg["business"]
    return {"business_name": b["name"], "website": b["website"], "booking_link": b["booking_link"],
            "sender_first_name": b["sender_first_name"], "offer": b["offer"],
            "services": "\n".join(f"- {s}" for s in b["services"])}


def template(cfg: dict) -> str:
    return cfg["message_template"].format_map(_Keep(values(cfg)))


def system_prompt(cfg: dict) -> str:
    return (PROMPT.read_text().format_map(_Keep(values(cfg), message_template=template(cfg)))
            + f"\n\nToday is {datetime.now():%B %d, %Y}. Always answer by calling record_commenter.")


def comment_message(lead: dict) -> str:
    return (f"Username: @{lead['username']}\nDisplay name: {lead.get('full_name') or '(none)'}\n"
            f"Their comment(s) on the post:\n\"\"\"\n{lead.get('text', '')[:2000]}\n\"\"\"")


def first_name(lead: dict) -> str:
    m = re.match(r"[A-Za-z][a-z'-]+", (lead.get("full_name") or "").strip())
    return m.group(0).capitalize() if m else ""


def plain(cfg: dict, lead: dict) -> dict:
    name = first_name(lead)
    dm = template(cfg).replace("{topic}", "college stuff")
    dm = dm.replace("{first_name}", name) if name else dm.replace(" {first_name}", "").replace("{first_name}", "")
    return {"relevant": True, "score": 100, "need": "(not checked: ai.enabled is false)", "likely_minor": False,
            "minor_evidence": "not checked", "first_name": name, "dm": dm, "model": None,
            "scored_at": datetime.now().isoformat(timespec="seconds")}


def assess(client, cfg: dict, system: str, lead: dict) -> dict:
    resp = client.messages.create(
        model=cfg["ai"]["model"],
        max_tokens=600,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "record_commenter"},
        messages=[{"role": "user", "content": comment_message(lead)}],
    )
    block = next(b for b in resp.content if b.type == "tool_use")
    return dict(block.input, model=cfg["ai"]["model"], scored_at=datetime.now().isoformat(timespec="seconds"))


def run(cfg: dict, rescore=False, dry_run=False) -> int:
    load_dotenv(HERE / ".env")
    leads, done, messaged = store.load_leads(), store.handled(), store.messaged()
    todo = [l for l in leads.values() if (rescore or "ai" not in l) and l["key"] not in done
            and l["username"].lower() not in messaged]
    use_ai = cfg["ai"]["enabled"]
    if dry_run:
        print(system_prompt(cfg) if use_ai else template(cfg))
        print("\n----- first commenter -----\n" + (comment_message(todo[0]) if todo else "(none to score)"))
        return 0
    if not todo:
        print("[+] Nothing new to score.")
        return 0
    if not use_ai:
        for lead in todo:
            lead["ai"] = plain(cfg, lead)
        store.save_leads(leads)
        print(f"[done] {len(todo)} commenters got the template (ai.enabled is false)")
        return len(todo)

    import anthropic
    client, system, hits = anthropic.Anthropic(), system_prompt(cfg), 0
    print(f"[+] Reading {len(todo)} comments with {cfg['ai']['model']}...")
    try:
        for i, lead in enumerate(todo, 1):
            try:
                lead["ai"] = assess(client, cfg, system, lead)
            except anthropic.AuthenticationError:
                sys.exit("ANTHROPIC_API_KEY is missing or invalid (set it in instagram/.env).")
            except anthropic.APIError as exc:
                print(f"[!] @{lead['username']}: {exc}; will retry next run")
                continue
            a = lead["ai"]
            good = a["relevant"] and a["score"] >= cfg["ai"]["min_score"]
            hits += good
            print(f"[{i}/{len(todo)}] {a['score']:>3} {'LEAD' if good else '    '}{' [minor]' if a['likely_minor'] else ''}"
                  f" @{lead['username']}: {lead.get('text', '')[:70]!r}")
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
    run(json.loads(CONFIG.read_text()), args.rescore, args.dry_run)


if __name__ == "__main__":
    main()

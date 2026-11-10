"""draft.py - draft answers, with Claude, to everyone who replied to our DMs on Reddit, Facebook and Instagram.

    python inbox/run.py draft [--limit N] [--dry-run]

Reads the three reply watchers' files (read only; nothing in the platform folders is changed):
    reddit/data/replies.csv     message_id, username, kind, received_at, subject, snippet, permalink, recorded_at
    facebook/data/replies.csv   author_key, author, post_key, seen_at, snippet, chat_url
    instagram/data/replies.csv  username, item_id, replied_at, snippet, thread_url, sent_at, found_at, how
each optionally with a `text` column (the full message; `snippet` is used when it's missing or empty).

Every reply with no entry in inbox/data/answers.json yet is drafted once:
    - replies from one person that no entry covers are grouped into one entry, keyed on the newest reply's id, and
      every id in the group is kept in the entry's `covers`. If that person already has an untouched draft
      (status 'drafted', text not edited), it is folded into the new one and dismissed as replaced.
    - our original DM comes from the platform's leads.json (the approved message, else the edited, else the drafted
      one), found through its contacted.csv (Reddit: the root contacted.csv)
    - a clear decline or "stop messaging me" is caught here without asking Claude; Claude can flag one too. Those are
      stored with suggest="dismiss" (status stays 'drafted', so store.answered doesn't count them) and no pitch.
    - otherwise Claude drafts the answer (prompts/answer.md, filled from config.json's business block)

At most ai.max_drafts_per_run entries per run (--limit overrides). --dry-run prints the prompt, the first
conversation and what would be drafted, and writes nothing. Needs ANTHROPIC_API_KEY in the environment or a .env
(inbox/.env, else the repo root's, facebook/'s, instagram/'s or reddit/'s).
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import store

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG = HERE / "config.json"
PROMPTS = HERE / "prompts"
ENV_FILES = [HERE / ".env", ROOT / ".env", ROOT / "facebook" / ".env", ROOT / "instagram" / ".env",
             ROOT / "reddit" / ".env"]
PATHS = {
    "reddit": {"replies": ROOT / "reddit" / "data" / "replies.csv", "leads": ROOT / "reddit" / "data" / "leads.json",
               "contacted": ROOT / "contacted.csv"},
    "facebook": {"replies": ROOT / "facebook" / "data" / "replies.csv",
                 "leads": ROOT / "facebook" / "data" / "leads.json",
                 "contacted": ROOT / "facebook" / "data" / "contacted.csv"},
    "instagram": {"replies": ROOT / "instagram" / "data" / "replies.csv",
                  "leads": ROOT / "instagram" / "data" / "leads.json",
                  "contacted": ROOT / "instagram" / "data" / "contacted.csv"},
}
PLATFORM_NAMES = {"reddit": "Reddit", "facebook": "Facebook Messenger", "instagram": "Instagram Direct"}
MAX_CHARS = 4000

TOOL = {
    "name": "record_answer",
    "description": "Record how to answer this person's reply.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": ["question", "interested", "thanks", "declined", "stop", "other"],
                       "description": "What their message(s) amount to"},
            "suggest_dismiss": {"type": "boolean",
                                "description": "True if they declined or asked not to be contacted"},
            "reason": {"type": "string", "description": "One sentence: what they want and why this answer"},
            "answer": {"type": "string", "description": "The reply to send; empty if none should be sent"},
        },
        "required": ["intent", "suggest_dismiss", "reason", "answer"],
    },
}

# ----------------------------------------------------------------- decline detection (no Claude needed)
STOP = re.compile(r"\b(stop (messag|texting|contact|dm|spamm|send)\w*|(do not|don'?t|dont|never) (message|contact|"
                  r"dm|text|msg|write to|reach out to) (me|us|my)|unsubscribe|leave (me|us) alone|stop it\b|"
                  r"this is spam|you'?re (a )?spam|spammer|reported|i'?ll report|blocking you|block(ed)? you|"
                  r"remove me|take me off|opt out|go away|f+u+c+k+ off)", re.I)
DECLINE = re.compile(r"\b(not interested|no thanks?|no,? thank(s| you)|nah\b|no need|not (looking|needed|for me)|"
                     r"i'?m (good|fine|all set|ok|okay)|we'?re (good|fine|all set|ok|okay)|all set|pass\b|"
                     r"already (have|got|working with|found) (a |an |my |our )?(counselor|consultant|someone|help|"
                     r"tutor|coach))", re.I)
YET = re.compile(r"\b(but|how|what|when|where|why|price|cost|\?)", re.I)


def decline_kind(text: str) -> str | None:
    """'stop' if they ask not to be contacted (anywhere in the text), 'declined' for a short, clear no (without a
    'but' or a question), else None. Conservative: anything unclear goes to Claude."""
    t = (text or "").strip()
    if not t:
        return None
    if STOP.search(t):
        return "stop"
    if len(t.split()) <= 25 and DECLINE.search(t) and not YET.search(t):
        return "declined"
    return None


# ----------------------------------------------------------------- reading the platforms' files
def _csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        return {}


def _text(row: dict) -> str:
    return ((row.get("text") or "").strip() or (row.get("snippet") or "").strip())


def read_replies(platform: str) -> list[dict]:
    """That platform's replies.csv as uniform rows: platform, reply_id, person, name, at, text, chat_url, extra."""
    out = []
    for r in _csv(PATHS[platform]["replies"]):
        if platform == "reddit":
            rid, person, name = r.get("message_id"), r.get("username"), r.get("username")
            at = r.get("received_at") or r.get("recorded_at") or ""
            url = r.get("permalink") or (f"https://www.reddit.com/message/compose/?to={person}" if person else "")
            extra = {"kind": r.get("kind") or "message", "subject": r.get("subject") or ""}
        elif platform == "facebook":
            person, name, at = r.get("author_key"), r.get("author") or r.get("author_key"), r.get("seen_at") or ""
            rid = f"{person}:{at}" if person else None
            url = r.get("chat_url") or (f"https://www.facebook.com/messages/t/{person}" if person else "")
            extra = {"post_key": r.get("post_key") or ""}
        else:
            rid, person, name = r.get("item_id"), r.get("username"), r.get("username")
            at = r.get("replied_at") or r.get("found_at") or ""
            url = r.get("thread_url") or (f"https://ig.me/m/{person}" if person else "")
            extra = {}
        if not rid or not person:
            continue
        out.append({"platform": platform, "reply_id": str(rid), "person": person, "name": name or person,
                    "at": at, "text": _text(r), "chat_url": url, "extra": extra})
    return out


def _dm_of(lead: dict, *names) -> str:
    """The message in a lead: the approved one, else your edit, else Claude's draft."""
    for block in ("approved", "edited", "ai"):
        b = lead.get(block)
        if isinstance(b, str) and block == "approved" and b.strip():
            return b.strip()
        if isinstance(b, dict):
            for n in names:
                if isinstance(b.get(n), str) and b[n].strip():
                    return b[n].strip()
    return ""


def _sent_rows(platform: str, who_field: str, person: str) -> list[dict]:
    want = person.strip().lower().removeprefix("u/")
    return [r for r in _csv(PATHS[platform]["contacted"])
            if r.get("status") == "sent" and (r.get(who_field) or "").strip().lower().removeprefix("u/") == want]


def original(platform: str, person: str, reply: dict | None = None) -> dict:
    """{'message': our DM to them ('' if it can't be found), 'name': a better display name or '', 'sent': whether
    contacted.csv records it sent}."""
    leads = _json(PATHS[platform]["leads"])
    key = person.strip().lower().removeprefix("u/")
    if platform == "reddit":
        mine = [l for l in leads.values() if isinstance(l, dict)
                and (l.get("author") or "").strip().lower().removeprefix("u/") == key]
        mine.sort(key=lambda l: (bool(l.get("approved")), str(l.get("created_at") or "")))
        msgs = [m for m in (_dm_of(l, "message", "dm") for l in reversed(mine)) if m]
        return {"message": msgs[0] if msgs else "", "name": "",
                "sent": bool(_sent_rows("reddit", "username", person))}
    if platform == "facebook":
        sent = _sent_rows("facebook", "author_key", person)
        keys = [(reply or {}).get("extra", {}).get("post_key")] + [r.get("post_key") for r in reversed(sent)]
        for k in keys:
            if k and k in leads and _dm_of(leads[k], "dm"):
                return {"message": _dm_of(leads[k], "dm"), "name": leads[k].get("author") or "", "sent": bool(sent)}
        for l in leads.values():
            if isinstance(l, dict) and (l.get("author_key") or "").lower() == key and _dm_of(l, "dm"):
                return {"message": _dm_of(l, "dm"), "name": l.get("author") or "", "sent": bool(sent)}
        return {"message": "", "name": "", "sent": bool(sent)}
    sent = _sent_rows("instagram", "username", person)
    for k in [r.get("lead_key") for r in reversed(sent)]:
        if k and k in leads and _dm_of(leads[k], "dm"):
            return {"message": _dm_of(leads[k], "dm"), "name": leads[k].get("full_name") or "", "sent": True}
    for l in leads.values():
        if isinstance(l, dict) and (l.get("username") or "").lower() == key and _dm_of(l, "dm"):
            return {"message": _dm_of(l, "dm"), "name": l.get("full_name") or "", "sent": bool(sent)}
    return {"message": "", "name": "", "sent": bool(sent)}


# ----------------------------------------------------------------- grouping
def pending() -> list[dict]:
    """Conversations to draft: one per person with replies no entry covers, oldest newest-message first.
    Each: platform, person, name, chat_url, reply_id (the newest), covers, replies (oldest first), replaces (the id
    of an untouched draft folded into this one, or '')."""
    answers = store.load()
    groups = []
    for platform in store.PLATFORMS:
        rows = read_replies(platform)
        by_id = {r["reply_id"]: r for r in rows}
        done = store._covered(answers, platform)
        fresh = {}
        for r in rows:
            if r["reply_id"] not in done:
                fresh.setdefault(r["person"].lower(), []).append(r)
        for who, new in fresh.items():
            replaces, old = "", []
            drafts = [a for a in answers.values() if a["platform"] == platform and a["person"].lower() == who
                      and a["status"] == "drafted" and a["text"] == a["draft"]]
            if drafts:
                prev = drafts[-1]
                replaces = prev["id"]
                old = [by_id[c] for c in prev.get("covers") or [prev["reply_id"]] if c in by_id]
            seen, msgs = set(), []
            for r in old + new:
                if r["reply_id"] not in seen:
                    seen.add(r["reply_id"])
                    msgs.append(r)
            msgs.sort(key=lambda r: r["at"])
            last = msgs[-1]
            groups.append({"platform": platform, "person": last["person"], "name": last["name"],
                           "chat_url": last["chat_url"], "reply_id": last["reply_id"],
                           "covers": [r["reply_id"] for r in msgs], "replies": msgs, "replaces": replaces,
                           "at": last["at"], "extra": last["extra"]})
    groups.sort(key=lambda g: g["at"])
    return groups


def their_message(replies: list[dict]) -> str:
    if len(replies) == 1:
        return replies[0]["text"]
    return "\n\n".join(f"[{(r['at'] or '')[:16].replace('T', ' ')}] {r['text']}" for r in replies)


def earlier_answers(platform: str, person: str) -> list[str]:
    return [a["text"] for a in store.of_person(platform, person)
            if a["status"] in ("sent", "sending", "uncertain") and a["text"].strip()]


# ----------------------------------------------------------------- Claude
def load_config() -> dict:
    return json.loads(CONFIG.read_text())


def tagged(cfg: dict, platform: str, person: str, text: str) -> str:
    """The draft with the plain booking link swapped for one tagged with where this answer goes
    (utm_source=<platform>, utm_content=<person>), so bookings/run.py can match a booking to this conversation."""
    link = cfg["business"]["booking_link"]
    if not link or link not in text:
        return text
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(link)
    query = dict(parse_qsl(parts.query), utm_source=platform, utm_medium="dm",
                 utm_content=person.strip().lower().removeprefix("u/").removeprefix("@"))
    return text.replace(link, urlunsplit(parts._replace(query=urlencode(query))))


def system_prompt(cfg: dict) -> str:
    b = cfg["business"]
    values = {"business_name": b["name"], "website": b["website"], "booking_link": b["booking_link"],
              "sender_first_name": b["sender_first_name"], "offer": b["offer"],
              "services": "\n".join(f"- {s}" for s in b["services"])}
    return (PROMPTS / "answer.md").read_text().format(**values) + f"\n\nToday is {datetime.now():%B %d, %Y}."


def user_message(platform: str, name: str, our_message: str, their: str, earlier: list[str] = (),
                 kind: str = "", hint: str = "") -> str:
    parts = [f"Platform: {PLATFORM_NAMES.get(platform, platform)}", f"Their name / username: {name or 'unknown'}"]
    if kind and kind != "message":
        parts.append(f"Note: this is a public {kind.replace('_', ' ')} on Reddit, not a private message. Our answer "
                     f"will be sent to them as a private message.")
    parts.append(f"Our original message to them:\n\"\"\"\n{our_message[:MAX_CHARS] or '(not found)'}\n\"\"\"")
    for i, e in enumerate(earlier, 1):
        parts.append(f"Our reply #{i} to them since then:\n\"\"\"\n{e[:MAX_CHARS]}\n\"\"\"")
    parts.append(f"What they wrote (oldest first):\n\"\"\"\n{their[:MAX_CHARS]}\n\"\"\"")
    if hint:
        parts.append(f"Instructions from the person reviewing this draft: {hint[:1000]}")
    return "\n\n".join(parts)


def load_env():
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for p in ENV_FILES:
        if p.exists():
            load_dotenv(p)               # never overrides a variable that is already set


def client():
    """The Anthropic client (tests patch this)."""
    load_env()
    import anthropic
    return anthropic.Anthropic()


def ask(cl, cfg: dict, system: str, message: str) -> dict:
    resp = cl.messages.create(
        model=cfg["ai"]["model"],
        max_tokens=cfg["ai"].get("max_tokens", 1000),
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "record_answer"},
        messages=[{"role": "user", "content": message}],
    )
    block = next(b for b in resp.content if b.type == "tool_use")
    out = dict(block.input)
    out["answer"] = (out.get("answer") or "").strip()
    if out.get("intent") == "stop":
        out["answer"] = ""
    return out


def decide(cl, cfg, system, platform, name, our, their, earlier=(), kind="", hint="", newest="") -> dict:
    """{'draft', 'suggest', 'intent', 'reason'}: the decline check on their newest message(s) first, then Claude."""
    caught = decline_kind(newest or their)
    if caught and not hint:
        return {"draft": "", "suggest": "dismiss", "intent": caught,
                "reason": "they asked not to be contacted" if caught == "stop" else "they declined"}
    a = ask(cl, cfg, system, user_message(platform, name, our, their, earlier, kind, hint))
    dismiss = bool(a.get("suggest_dismiss")) or a.get("intent") in ("declined", "stop")
    return {"draft": a["answer"], "suggest": "dismiss" if dismiss else "", "intent": a.get("intent", "other"),
            "reason": a.get("reason", "")}


def draft_group(cl, cfg: dict, system: str, g: dict) -> dict | None:
    orig = original(g["platform"], g["person"], g["replies"][-1])
    name = orig["name"] or g["name"]
    their = their_message(g["replies"])
    old = _old_ids(g)
    newest = " ".join(r["text"] for r in g["replies"] if r["reply_id"] not in old)
    d = decide(cl, cfg, system, g["platform"], name, orig["message"], their,
               earlier_answers(g["platform"], g["person"]), g["extra"].get("kind", ""), newest=newest)
    note = d["reason"]
    if not orig["message"]:
        note = (note + "; " if note else "") + "our original DM wasn't found"
    entry = store.add_entry(g["platform"], g["reply_id"], person=g["person"], name=name, chat_url=g["chat_url"],
                            their_message=their, our_message=orig["message"],
                            draft=tagged(cfg, g["platform"], g["person"], d["draft"]),
                            covers=g["covers"], suggest=d["suggest"], intent=d["intent"], note=note,
                            received_at=g["at"], **{k: v for k, v in g["extra"].items() if v})
    if entry and g["replaces"]:
        try:
            store.update(g["replaces"], expect=("drafted",), status="dismissed",
                         reason=f"replaced by {entry['id']}: they wrote again")
        except (KeyError, ValueError):
            pass
    return entry


def _old_ids(g: dict) -> set:
    if not g["replaces"]:
        return set()
    old = store.get(g["replaces"]) or {}
    return set(old.get("covers") or [old.get("reply_id")])


def redraft(aid: str, hint: str = "", cl=None, cfg: dict | None = None) -> dict:
    """Ask Claude again for one entry (the page's Re-draft). Only entries in store.EDITABLE; the result is back in
    'drafted' so it's reviewed before sending. Raises KeyError / ValueError like store.update."""
    a = store.get(aid)
    if a is None:
        raise KeyError(aid)
    if a["status"] not in store.EDITABLE:
        raise ValueError(f"{aid} is {a['status']!r}, not {' / '.join(store.EDITABLE)}")
    cfg = cfg or load_config()
    cl = cl or client()
    earlier = earlier_answers(a["platform"], a["person"])
    d = decide(cl, cfg, system_prompt(cfg), a["platform"], a["name"], a["our_message"], a["their_message"],
               earlier, a.get("kind", ""), hint=hint)
    if not d["draft"] and d["suggest"] != "dismiss":
        raise ValueError("Claude returned an empty answer; try again")
    text = tagged(cfg, a["platform"], a["person"], d["draft"])
    return store.update(aid, expect=store.EDITABLE, draft=text, text=text, status="drafted",
                        suggest=d["suggest"], intent=d["intent"], note=d["reason"], reason="")


# ----------------------------------------------------------------- the run
def run(limit: int | None = None, dry_run: bool = False, cl=None, cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_config()
    todo = pending()
    cap = cfg["ai"].get("max_drafts_per_run", 40) if limit is None else limit
    batch = todo[:cap]
    system = system_prompt(cfg)
    if dry_run:
        print(system, "\n\n----- first conversation -----\n")
        if batch:
            g = batch[0]
            orig = original(g["platform"], g["person"], g["replies"][-1])
            print(user_message(g["platform"], orig["name"] or g["name"], orig["message"], their_message(g["replies"]),
                               earlier_answers(g["platform"], g["person"]), g["extra"].get("kind", "")))
        else:
            print("(nothing new to answer)")
        print(f"\n----- would draft {len(batch)} of {len(todo)} -----")
        for g in batch:
            flag = decline_kind(their_message(g["replies"]))
            print(f"  {g['platform']:<9} {g['name']:<28} {len(g['replies'])} message(s)"
                  f"{'  [suggest dismiss: ' + flag + ']' if flag else ''}{'  (replaces ' + g['replaces'] + ')' if g['replaces'] else ''}")
        return []
    if not batch:
        print("[+] No new replies to answer.")
        return []
    if cl is None and any(not decline_kind(their_message(g["replies"])) for g in batch):
        try:
            cl = client()
        except Exception as exc:                     # noqa: BLE001 - e.g. no API key at all
            sys.exit(f"Can't start the Claude client ({exc}). Set ANTHROPIC_API_KEY in inbox/.env.")
    print(f"[+] Drafting {len(batch)} answers{f' (of {len(todo)} waiting)' if len(todo) > len(batch) else ''} "
          f"with {cfg['ai']['model']}...")
    made = []
    for i, g in enumerate(batch, 1):
        try:
            entry = draft_group(cl, cfg, system, g)
        except Exception as exc:                     # noqa: BLE001 - one bad conversation mustn't stop the rest
            if type(exc).__name__ == "AuthenticationError":
                sys.exit("ANTHROPIC_API_KEY is missing or invalid (set it in inbox/.env).")
            print(f"[!] {g['platform']} {g['name']}: {exc}; will retry next run")
            continue
        if entry:
            made.append(entry)
            tag = "suggest dismiss" if entry.get("suggest") == "dismiss" else entry.get("intent", "")
            print(f"[{i}/{len(batch)}] {g['platform']:<9} {entry['name']:<26} {tag}")
    print(f"[done] {len(made)} drafts. Review them: python inbox/run.py serve")
    return made

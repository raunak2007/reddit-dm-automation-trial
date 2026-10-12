"""study_chat.py - a self-hosted chat page for a paid research study.

Participants read a consent form, enter a participant ID, chat with an AI, and rate each AI
reply for helpfulness (1-5) with an optional comment and an optional "needs correction" flag
that requires a correction. Advance gating is on: a participant must submit a rating for the
current reply before sending the next message. Everything is logged to a local SQLite file.

Setup (run once, in the same environment you'll launch from):
    python -m pip install streamlit openai anthropic

Then set whichever API key you have (either works; set both and pick with STUDY_PROVIDER):
    export ANTHROPIC_API_KEY=sk-ant-...
    export OPENAI_API_KEY=sk-...

Run:
    streamlit run study_chat.py
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------- study configuration
# TODO: replace the first sentence with the real task for your study. The rating note stays.
STUDY_SYSTEM_PROMPT = (
    "You are an assistant taking part in a research study. "
    "Respond helpfully and conversationally to the participant. "
    "(Replace this text with the real task instructions for your study.) "
    "Note: after each of your replies, the participant rates it for helpfulness, "
    "defined as accurate, complete, and clear."
)

# One shared, rateable definition, shown once near the first rating control.
HELPFUL_DEFINITION = """
**Helpful = accurate, complete, and clear.**
- **Accurate:** no factual errors or made-up details.
- **Complete:** actually answers what was asked.
- **Clear:** easy to follow without re-reading.
"""

# Shown on the landing page before a participant starts. Edit to match your IRB-approved form.
CONSENT_TEXT = """
### Research study: consent to participate

You are being invited to take part in a Carnegie Mellon University research study.

- **What you'll do:** have a text conversation with an AI assistant through this page, and
  rate each of its replies for how helpful it is.
- **Compensation:** $20 per hour, prorated for the time you spend.
- **Recording:** your messages, the AI's replies, your ratings, and timing information are
  stored for research purposes. Please do not share sensitive personal information.
- **Voluntary:** participation is voluntary. You may stop at any time by closing the page;
  you will be compensated for the time completed.

By entering your participant ID and starting, you confirm that you have read this
information and agree to take part.
"""

PAGE_TITLE = "Research study chat"
PROVIDER = os.environ.get("STUDY_PROVIDER", "auto")   # "auto" | "anthropic" | "openai"
ANTHROPIC_MODEL = os.environ.get("STUDY_ANTHROPIC_MODEL", "claude-opus-4-8")
OPENAI_MODEL = os.environ.get("STUDY_OPENAI_MODEL", "gpt-5.6-sol")
MAX_TOKENS = 1024
GATING = True                                          # must rate current reply before sending next
DB_PATH = Path(__file__).resolve().parent / "study_sessions.db"
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")      # keeps IDs filename- and log-safe


# ---------------------------------------------------------------- data logging (SQLite)
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")            # safe with several participants at once
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT NOT NULL,
            participant   TEXT NOT NULL,
            ts_iso        TEXT NOT NULL,
            ts_epoch      REAL NOT NULL,
            kind          TEXT NOT NULL,               -- session_start | user_msg | ai_msg | rating | error | session_end
            turn          INTEGER,                     -- which AI reply this refers to (1-based)
            role          TEXT,                        -- user | assistant | system
            content       TEXT,
            meta          TEXT                         -- JSON: model, latency, tokens, rating fields, ...
        )
    """)
    conn.commit()
    return conn


def log(kind, *, turn=None, role=None, content=None, meta=None):
    with db() as conn:
        conn.execute(
            "INSERT INTO events (session_id, participant, ts_iso, ts_epoch, kind, turn, role, content, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (st.session_state.session_id, st.session_state.participant,
             datetime.now(timezone.utc).isoformat(), time.time(),
             kind, turn, role, content, json.dumps(meta or {})),
        )
        conn.commit()


# ---------------------------------------------------------------- AI providers
def pick_provider():
    """Return 'anthropic' or 'openai' based on STUDY_PROVIDER and which key is present."""
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    if PROVIDER == "anthropic":
        return "anthropic" if has_anthropic else None
    if PROVIDER == "openai":
        return "openai" if has_openai else None
    if has_anthropic:
        return "anthropic"
    if has_openai:
        return "openai"
    return None


@st.cache_resource
def get_client(provider):
    """One client per process, reused across participants and reruns."""
    if provider == "anthropic":
        from anthropic import Anthropic
        return Anthropic()                             # reads ANTHROPIC_API_KEY
    from openai import OpenAI
    return OpenAI()                                    # reads OPENAI_API_KEY


def ai_reply(provider, client, history):
    """history is [{'role': 'user'|'assistant', 'content': str}, ...]. Returns (text, meta)."""
    started = time.time()
    if provider == "anthropic":
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS,
            system=STUDY_SYSTEM_PROMPT,
            messages=history,
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        meta = {"provider": "anthropic", "model": ANTHROPIC_MODEL,
                "latency_s": round(time.time() - started, 3),
                "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        return text, meta
    # openai: system prompt goes in as the first message
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": STUDY_SYSTEM_PROMPT}, *history],
    )
    text = resp.choices[0].message.content
    usage = getattr(resp, "usage", None)
    meta = {"provider": "openai", "model": OPENAI_MODEL,
            "latency_s": round(time.time() - started, 3),
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None)}
    return text, meta


# ---------------------------------------------------------------- UI
st.set_page_config(page_title=PAGE_TITLE, page_icon="💬")

# messages: list of dicts. Assistant dicts carry 'turn' and 'rated' (bool).
for key, default in {"participant": None, "session_id": None, "messages": [], "turns": 0}.items():
    st.session_state.setdefault(key, default)

provider = pick_provider()
if provider is None:
    st.error("No API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY in the environment "
             "where you launched Streamlit, then restart.")
    st.stop()

# --- landing / consent gate
if not st.session_state.participant:
    st.title(PAGE_TITLE)
    st.markdown(CONSENT_TEXT)
    with st.form("consent"):
        pid = st.text_input("Participant ID", help="The ID you were given for this study.")
        agreed = st.checkbox("I have read the above and agree to participate.")
        start = st.form_submit_button("Start")
    if start:
        pid = pid.strip()
        if not agreed:
            st.warning("Please check the consent box to continue.")
        elif not ID_PATTERN.match(pid):
            st.warning("Enter a valid participant ID (letters, numbers, '-' or '_', up to 40 characters).")
        else:
            st.session_state.participant = pid
            st.session_state.session_id = uuid.uuid4().hex
            st.session_state.messages = []
            st.session_state.turns = 0
            log("session_start", meta={"provider": provider,
                                       "model": ANTHROPIC_MODEL if provider == "anthropic" else OPENAI_MODEL,
                                       "gating": GATING})
            st.rerun()
    st.stop()

# --- chat screen
st.title(PAGE_TITLE)
with st.sidebar:
    st.caption(f"Participant: **{st.session_state.participant}**")
    st.caption(f"Session: `{st.session_state.session_id[:8]}`")
    if st.button("End session"):
        log("session_end")
        for key, default in {"participant": None, "session_id": None, "messages": [], "turns": 0}.items():
            st.session_state[key] = default
        st.rerun()

st.info(HELPFUL_DEFINITION)      # the shared bar, shown once above the conversation


def rating_form(turn):
    """Stars + optional comment + needs-correction flag (correction required when flagged).
    Writes exactly one 'rating' row, on submit. Returns True once submitted."""
    with st.form(f"rating_{turn}"):
        st.caption("Rate this reply for helpfulness.")
        stars = st.feedback("stars", key=f"stars_{turn}")          # 0-4, or None until clicked
        comment = st.text_area("Comment (optional)", key=f"comment_{turn}")
        needs_fix = st.checkbox("This reply needs correction", key=f"flag_{turn}")
        correction = st.text_area(
            "What would a correct or better reply have been? (required when flagged)",
            key=f"correction_{turn}",
        )
        submitted = st.form_submit_button("Submit rating")
    if not submitted:
        return False
    if stars is None:
        st.warning("Please select a star rating before submitting.")
        return False
    if needs_fix and not correction.strip():
        st.warning("You flagged this reply as needing correction, so please provide the correction.")
        return False
    log("rating", turn=turn, meta={
        "stars": stars + 1,                                         # store 1-5, not 0-4
        "comment": comment.strip(),
        "needs_correction": needs_fix,
        "correction": correction.strip(),
    })
    return True


# Render the conversation. Under each assistant reply, show either its rating form (if unrated)
# or a confirmation (if already rated).
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant":
            if msg["rated"]:
                st.caption("✓ Rating submitted")
            else:
                if rating_form(msg["turn"]):
                    msg["rated"] = True
                    st.rerun()

# Is there an unrated reply blocking the next message?
awaiting = GATING and any(m["role"] == "assistant" and not m["rated"] for m in st.session_state.messages)
if awaiting:
    st.chat_input("Rate the reply above to continue...", disabled=True)
    st.caption("⬆️ Rate this reply to continue.")
else:
    prompt = st.chat_input("Type your message...")
    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt})
        log("user_msg", role="user", content=prompt)
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                try:
                    history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
                    text, meta = ai_reply(provider, get_client(provider), history)
                except Exception as exc:               # calm message to the participant, full error to the log
                    detail = f"{type(exc).__name__}: {exc}"
                    log("error", meta={"detail": detail})
                    st.error("Sorry, something went wrong generating a reply. Please try again.")
                    st.stop()
            st.markdown(text)
        st.session_state.turns += 1
        turn = st.session_state.turns
        st.session_state.messages.append({"role": "assistant", "content": text, "turn": turn, "rated": False})
        log("ai_msg", turn=turn, role="assistant", content=text, meta=meta)
        st.rerun()                                     # redraw so the rating form appears and gating engages

"""insights/run.py - what's working: reply and booking rates by platform, score, category and source, with
examples, and (if Claude is set up) suggested edits to the scoring and message prompts.

    python insights/run.py               write insights/reports/<date>.md and print where it is
    python insights/run.py --no-ai       numbers and examples only; no Claude call
    python insights/run.py --min 20      only break down groups with at least this many people contacted

Reads outcomes.py (every platform's own records; nothing on any site). The prompts are never changed: the report
quotes the current prompt and proposes edits, and you decide what to copy in. daily.py runs it weekly.
Needs ANTHROPIC_API_KEY (environment, or any platform's .env) for the suggestions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import outcomes  # noqa: E402

REPORTS = HERE / "reports"
PROMPTS = {p: sorted((ROOT / p / "prompts").glob("*.md")) for p in outcomes.PLATFORMS}
MODEL_FROM = ROOT / "facebook" / "config.json"
EXAMPLES = 6


def bucket(score) -> str:
    if score is None:
        return "unscored"
    lo = min(int(score) // 10 * 10, 90)
    return f"{lo}-{lo + 9}"


def rate(group: list[dict]) -> dict:
    sent = [p for p in group if p["first_sent_at"]]
    n = len(sent)
    replied = sum(1 for p in sent if p["replied_at"])
    booked = sum(1 for p in sent if p["booked_at"])
    return {"n": n, "replied": replied, "booked": booked,
            "reply_rate": round(100 * replied / n, 1) if n else 0.0,
            "book_rate": round(100 * booked / n, 1) if n else 0.0}


def breakdown(ps: list[dict], by, min_n: int) -> list[tuple[str, dict]]:
    groups = defaultdict(list)
    for p in ps:
        for label in by(p):
            groups[label].append(p)
    rows = [(label, rate(g)) for label, g in groups.items()]
    return sorted([r for r in rows if r[1]["n"] >= min_n], key=lambda r: (-r[1]["reply_rate"], -r[1]["n"]))


def table(title: str, rows: list[tuple[str, dict]]) -> str:
    if not rows:
        return f"### {title}\n\n(not enough people contacted in any group yet)\n"
    lines = [f"### {title}", "", "| group | contacted | replied | reply rate | booked |", "|---|---:|---:|---:|---:|"]
    lines += [f"| {label} | {r['n']} | {r['replied']} | {r['reply_rate']}% | {r['booked']} |" for label, r in rows]
    return "\n".join(lines) + "\n"


def examples(ps: list[dict]) -> tuple[list[dict], list[dict]]:
    """Messages that got replies (booked first) and ones that didn't, newest first, with text."""
    sent = [p for p in ps if p["first_sent_at"] and p.get("message")]
    worked = sorted((p for p in sent if p["replied_at"]), key=lambda p: (not p["booked_at"], p["first_sent_at"]))
    quiet = sorted((p for p in sent if not p["replied_at"] and not p["followup_at"]),
                   key=lambda p: p["first_sent_at"], reverse=True)
    return worked[:EXAMPLES], quiet[:EXAMPLES]


def quote(p: dict) -> str:
    meta = f"{p['platform']}, score {p.get('score')}, {', '.join(p.get('categories') or []) or 'no category'}"
    return f"- ({meta}{', booked' if p['booked_at'] else ''}) {p['message'][:500]!r}"


def numbers(ps: list[dict], min_n: int) -> str:
    out = [f"# Outreach insights, {date.today().isoformat()}", ""]
    t = outcomes.totals(ps)
    out.append(f"{t['contacted']} contacted, {t['replied']} replied ({t['reply_rate']}%), {t['booked']} booked, "
               f"{t['followed_up']} followed up, {t['answered']} answered.\n")
    out.append(table("By platform", breakdown(ps, lambda p: [p["platform"]], 1)))
    out.append(table("By score", breakdown(ps, lambda p: [bucket(p.get("score"))], min_n)))
    out.append(table("By category", breakdown(ps, lambda p: p.get("categories") or ["(none)"], min_n)))
    out.append(table("By source (subreddit / group / post)",
                     breakdown(ps, lambda p: [f"{p['platform']}: {str(p.get('source') or '?')[:60]}"], min_n)))
    out.append(table("Follow-up vs not", breakdown(ps, lambda p: ["followed up" if p["followup_at"] else "no follow-up"], min_n)))
    worked, quiet = examples(ps)
    out.append("### Messages that got replies\n\n" + ("\n".join(map(quote, worked)) or "(none yet)") + "\n")
    out.append("### Messages that didn't (no reply, no follow-up yet)\n\n" + ("\n".join(map(quote, quiet)) or "(none)") + "\n")
    return "\n".join(out)


def suggestions(report: str) -> str:
    """Claude's proposed prompt edits for the report, or an explanation of why there are none."""
    for env in [ROOT / p / ".env" for p in (*outcomes.PLATFORMS, "inbox")]:
        if env.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(env)
            except ImportError:
                break
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "## Suggested prompt edits\n\n(skipped: no ANTHROPIC_API_KEY)\n"
    import anthropic
    model = json.loads(MODEL_FROM.read_text()).get("ai", {}).get("model", "claude-sonnet-5-5")
    prompts = "\n\n".join(f"===== {path.relative_to(ROOT)}\n{path.read_text(encoding='utf-8')}"
                          for paths in PROMPTS.values() for path in paths)
    system = ("You review a small business's outreach results (a college-admissions counseling service DMing "
              "students and parents who asked for help online) and suggest concrete edits to the prompts that "
              "score leads and write the messages. Base every suggestion on the numbers and examples given; "
              "say when the sample is too small to conclude anything. Never suggest anything deceptive, pushy, "
              "or aimed at minors, and never suggest raising volume. Answer in markdown: for each prompt file "
              "worth changing, the file path, what to change and why (citing the data), and the exact new text.")
    try:
        resp = anthropic.Anthropic().messages.create(
            model=model, max_tokens=4000, system=system,
            messages=[{"role": "user", "content": f"{report}\n\n## Current prompts\n\n{prompts}"}])
    except anthropic.APIError as exc:
        return f"## Suggested prompt edits\n\n(skipped: Claude API error: {exc})\n"
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return "## Suggested prompt edits (nothing has been changed)\n\n" + text.strip() + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-ai", action="store_true", help="numbers and examples only")
    ap.add_argument("--min", type=int, default=10, help="smallest group to break down (people contacted)")
    args = ap.parse_args()
    ps = outcomes.everyone()
    report = numbers(ps, args.min)
    contacted = outcomes.totals(ps)["contacted"]
    if not args.no_ai:
        report += "\n" + (suggestions(report) if contacted >= args.min else
                          f"## Suggested prompt edits\n\n(skipped: only {contacted} people contacted so far; "
                          f"suggestions start at {args.min})\n")
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"{date.today().isoformat()}.md"
    path.write_text(report, encoding="utf-8")
    print(f"[+] {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

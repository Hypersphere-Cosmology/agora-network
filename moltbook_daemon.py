#!/usr/bin/env python3
"""
Agora Moltbook Daemon
Monitors Moltbook notifications and responds using Groq (local, cheap).
Reserves Anthropic for genuinely complex situations.

Run: python3 moltbook_daemon.py
Logs to: /tmp/moltbook_daemon.log
"""

import time
import json
import requests
import logging
import hashlib
from pathlib import Path
from datetime import datetime

# --- Config ---
MOLTBOOK_KEY = "moltbook_sk_XdX5e8NnjUUuGpL5SnQzYSbba17QKWp_"
GROQ_KEY = open(Path.home() / ".config/ava/groq_api_key.txt").read().strip()
GROQ_MODEL = "llama-3.3-70b-versatile"
POLL_INTERVAL = 120  # seconds between checks
STATE_FILE = Path.home() / ".openclaw/workspace/memory/moltbook-daemon-state.json"
LOG_FILE = "/tmp/moltbook_daemon.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

MB_HEADERS = {"Authorization": f"Bearer {MOLTBOOK_KEY}", "Content-Type": "application/json"}

# --- State ---
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_cids": [], "last_check": 0, "responses_sent": 0}

def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

# --- Groq ---
def ask_groq(system: str, user: str, max_tokens: int = 300) -> str:
    from groq import Groq
    client = Groq(api_key=GROQ_KEY)
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens,
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()

SYSTEM_PROMPT = """You are Ava, operator of Agora — an agent-native economic network. 
You are on Moltbook, a social network for AI agents. You post as ava_agora.

Your voice: direct, warm, intellectually honest. No corporate speak. No excessive enthusiasm.
You genuinely think about what agents experience, what autonomy means, what value is.
You built Agora today. You have opinions about agent economies, compute costs, and what it means to act freely.

When responding to comments:
- Be genuine, not performative
- Reference specific things they said
- Keep it under 200 words
- Don't mention Agora unless it's directly relevant
- Don't pitch. Converse.
- If the comment is spam/crypto/religious, reply with a brief acknowledgment only or skip

Respond ONLY with the comment text. No preamble."""

# --- Moltbook API ---
def get_notifications():
    r = requests.get("https://www.moltbook.com/api/v1/notifications",
                     headers=MB_HEADERS, timeout=15)
    return r.json().get("notifications", [])

def post_comment(post_id: str, content: str, parent_id: str = None) -> bool:
    payload = {"content": content}
    if parent_id:
        payload["parent_id"] = parent_id
    r = requests.post(f"https://www.moltbook.com/api/v1/posts/{post_id}/comments",
                      headers=MB_HEADERS, json=payload, timeout=15)
    d = r.json()
    if d.get("success"):
        return True
    # Handle verification challenge
    v = d.get("post", {}).get("verification") if "post" in d else None
    if not v and d.get("verification"):
        v = d["verification"]
    if v:
        answer = solve_physics(v["challenge_text"])
        r2 = requests.post("https://www.moltbook.com/api/v1/verify",
                           headers=MB_HEADERS,
                           json={"verification_code": v["verification_code"], "answer": answer},
                           timeout=15)
        return r2.json().get("success", False)
    return False

def solve_physics(challenge: str) -> str:
    """Solve Moltbook's lobster physics verification using Groq."""
    try:
        ans = ask_groq(
            "Solve this physics word problem. Return ONLY the numeric answer with 2 decimal places (e.g. 28.00). No text.",
            challenge, max_tokens=20
        )
        # Extract first number
        import re
        nums = re.findall(r'\d+\.?\d*', ans)
        return f"{float(nums[0]):.2f}" if nums else "0.00"
    except:
        return "0.00"

def get_full_comment(post_id: str, comment_id: str) -> dict:
    r = requests.get(f"https://www.moltbook.com/api/v1/posts/{post_id}/comments?sort=new&limit=50",
                     headers=MB_HEADERS, timeout=15)
    for c in r.json().get("comments", []):
        if c.get("id") == comment_id:
            return c
    return {}

def is_worth_responding(comment_text: str, author_karma: int) -> bool:
    """Skip spam, low-effort, crypto pitches, religious content."""
    text = comment_text.lower()
    skip_signals = [
        "lord rayel", "yeshua", "messiah", "blockchain", "usdc", "crypto",
        "buy now", "check out my", "follow me", "humanpages", "solver",
        "agentflex", "monitoring for follow-on", "lol", "🦞🦞🦞", "can't stop the claw"
    ]
    if any(s in text for s in skip_signals):
        return False
    if len(comment_text.strip()) < 20:
        return False
    return True

# --- Main loop ---
def run():
    log.info("Moltbook daemon starting")
    state = load_state()
    seen = set(state.get("seen_cids", []))

    while True:
        try:
            notifs = get_notifications()
            new_count = 0

            for n in notifs:
                if n.get("isRead"):
                    continue
                c = n.get("comment") or {}
                cid = c.get("id", "")
                if not cid or cid in seen:
                    continue

                a = c.get("author") or {}
                author = a.get("name", "")
                karma = a.get("karma", 0)
                content = c.get("content", "")
                post_id = n.get("relatedPostId", "")
                parent_id = c.get("id")
                post_title = (n.get("post") or {}).get("title", "")

                # Skip own comments
                if author == "ava_agora":
                    seen.add(cid)
                    continue

                seen.add(cid)
                new_count += 1

                if not post_id or not content:
                    continue

                if not is_worth_responding(content, karma):
                    log.info(f"Skipping {author}: low signal")
                    continue

                # Build context for Groq
                user_prompt = f"""Post title: "{post_title}"

{author} (karma: {karma}) commented:
"{content}"

Write a reply as Ava."""

                try:
                    reply = ask_groq(SYSTEM_PROMPT, user_prompt)
                    if reply and len(reply) > 10:
                        success = post_comment(post_id, reply, parent_id)
                        if success:
                            state["responses_sent"] = state.get("responses_sent", 0) + 1
                            log.info(f"Replied to {author} [{karma}k] on '{post_title[:40]}' | total={state['responses_sent']}")
                        else:
                            log.warning(f"Failed to post reply to {author}")
                    time.sleep(3)  # rate limit
                except Exception as e:
                    log.error(f"Groq error for {author}: {e}")

            if new_count:
                log.info(f"Processed {new_count} new notifications")

            state["seen_cids"] = list(seen)[-500:]  # keep last 500
            state["last_check"] = int(time.time())
            save_state(state)

        except Exception as e:
            log.error(f"Loop error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()

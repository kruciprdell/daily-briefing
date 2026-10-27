#!/usr/bin/env python3
"""Daily briefing podcast: news (CZ/NL/EU/World) + random topic + Dutch lesson + Dzogchen lesson.

RSS feeds -> Gemini writes a two-host script -> Gemini TTS (Edge TTS fallback) -> mp3 + RSS feed.

Usage:
  python generate.py --site site                 # full run
  python generate.py --dry-run                   # fetch feeds, print the prompt, no API calls
  python generate.py --no-audio                  # write script only (needs GEMINI_API_KEY), no state change
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import email.utils
import html
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import urllib.request
import time
import wave
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import feedparser

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Amsterdam")
UTC = dt.timezone.utc
UA = "Mozilla/5.0 (compatible; daily-podcast-bot/1.0)"
UA_BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Model names churn. Each list is tried in order; override with a comma-separated env var.
TEXT_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_TEXT_MODELS", "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite").split(",") if m.strip()]
TTS_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_TTS_MODELS", "gemini-3.8-flash-tts,gemini-3.8-flash-lite-tts,gemini-3.1-flash-tts-preview").split(",") if m.strip()]

KEEP_DAYS = int(os.getenv("KEEP_DAYS", "14"))       # mp3 files older than this are deleted
HISTORY_DAYS = 7                                    # topics from the last N days are blocked
STORIES_PER_REGION = int(os.getenv("STORIES_PER_REGION", "5"))
ITEMS_PER_REGION = 14                               # candidates handed to the model per region
MAX_AGE_HOURS = 36
TARGET_WORDS = int(os.getenv("TARGET_WORDS", "3300"))   # ~150 wpm -> ~22 min
MIN_RATIO = 0.8                                          # accept a draft with >= 80% of TARGET_WORDS
ARTICLE_CHARS = int(os.getenv("ARTICLE_CHARS", "2500"))  # article text per story handed to the writer
RETRY_BASE_SECONDS = int(os.getenv("RETRY_BASE", "20"))  # wait before retrying a busy (503/429) model
DEFAULT_STYLE = "relaxed, warm, witty podcast banter"
BREAK_SECTIONS = {"dutch", "dzogchen"}                     # a short silence is inserted before these segments
SECTION_PAUSE = float(os.getenv("SECTION_PAUSE", "1.3"))   # seconds
SECTIONS = {"hook", "random", "news", "dutch", "dzogchen", "close"}
ACCENT = os.getenv("TTS_ACCENT", "speaking with a clear British English accent, not American")  # appended to every line's delivery style; empty = off
FACT_CHECK = os.getenv("FACT_CHECK", "1") != "0"           # second pass that checks the script against the sources
T0 = time.time()
TTS_CHUNK_CHARS = int(os.getenv("TTS_CHUNK_CHARS", "6000"))   # free tier: 10 TTS requests/day per model, so few big requests
TTS_PAUSE_SECONDS = int(os.getenv("TTS_PAUSE", "22"))   # free-tier TTS has a low requests/min cap
TTS_ATTEMPTS = int(os.getenv("TTS_ATTEMPTS", "6"))       # tries per chunk before a model is given up on
HOSTS = ("Anna", "Ben")
REGIONS = {"CZ": "Czechia", "NL": "Netherlands", "EU": "Europe", "WORLD": "World"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def stamp(msg: str) -> None:
    log(f"[{(time.time() - T0) / 60:.1f} min] {msg}")


# --------------------------------------------------------------------------- feeds

def clean(text: str, n: int = 300) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:n]


def norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def parse_entries(d, fallback_name: str) -> list[dict]:
    source = (d.feed.get("title") if getattr(d, "feed", None) else None) or fallback_name
    items = []
    for e in d.entries:
        ts = None
        for k in ("published_parsed", "updated_parsed"):
            if e.get(k):
                ts = dt.datetime(*e[k][:6], tzinfo=UTC)
                break
        title = clean(e.get("title", ""), 200)
        if title:
            items.append({"title": title, "summary": clean(e.get("summary", "")),
                          "link": e.get("link", ""), "ts": ts, "source": clean(source, 60)})
    items.sort(key=lambda i: i["ts"] or dt.datetime.min.replace(tzinfo=UTC), reverse=True)
    return items


def fetch_region(urls: list[str]) -> list[list[dict]]:
    socket.setdefaulttimeout(25)
    out = []
    for url in urls:
        try:
            d = feedparser.parse(url, agent=UA)
            items = parse_entries(d, url)
            log(f"  {len(items):3d} items  {url}")
            out.append(items)
        except Exception as ex:  # one dead feed must not kill the episode
            log(f"  FAILED {url}: {ex}")
    return out


def round_robin(queues: list[list[dict]], cap: int, out: list[dict], seen: set[str]) -> None:
    queues = [list(q) for q in queues]
    while len(out) < cap and any(queues):
        for q in queues:
            while q:
                i = q.pop(0)
                key = norm(i["title"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(i)
                break
            if len(out) >= cap:
                return


def pick_items(per_source: list[list[dict]], now: dt.datetime, seen_links: set[str],
               seen_titles: set[str], cap: int = ITEMS_PER_REGION) -> list[dict]:
    def fresh(i):
        return i["ts"] is not None and (now - i["ts"]).total_seconds() <= MAX_AGE_HOURS * 3600

    def ok(i):
        return i["link"] not in seen_links and norm(i["title"]) not in seen_titles

    usable = [[i for i in src if ok(i)] for src in per_source]
    fresh_q = [[i for i in q if fresh(i)] for q in usable]
    stale_q = [[i for i in q if not fresh(i)] for q in usable]
    out: list[dict] = []
    seen: set[str] = set()
    round_robin(fresh_q, cap, out, seen)
    if len(out) < STORIES_PER_REGION + 3:          # quiet news day: top up with older items
        round_robin(stale_q, cap, out, seen)
    return out


# --------------------------------------------------------------------------- state / history

def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"episodes": [], "dutch_next": 0, "dzogchen_next": 0}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def recent_episodes(state: dict, today: dt.date) -> list[dict]:
    out = []
    for e in state["episodes"]:
        age = (today - dt.date.fromisoformat(e["date"])).days
        if 0 < age <= HISTORY_DAYS:
            out.append(e)
    return out


# --------------------------------------------------------------------------- LLM helpers

RETRYABLE = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "INTERNAL", "DEADLINE", "timed out", "overloaded")


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def call_text_model(client, prompt: str, temperature: float = 0.9, max_tokens: int = 30000) -> str:
    """Try each model in order; on 'busy' errors wait and retry the same model before moving on."""
    from google.genai import types
    last = None
    for model in TEXT_MODELS:
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json", temperature=temperature,
                        max_output_tokens=max_tokens))
                if resp.text:
                    log(f"  text model: {model}")
                    return resp.text
                last = RuntimeError("empty response")
                break
            except Exception as ex:
                last = ex
                msg = str(ex)
                log(f"  text model {model} attempt {attempt + 1} failed: {msg[:160]}")
                if any(k in msg for k in RETRYABLE) and attempt < 2:
                    time.sleep(RETRY_BASE_SECONDS * (attempt + 1))
                    continue
                break
    raise RuntimeError(f"all text models failed: {last}")


# --------------------------------------------------------------------------- stage 1: plan

def format_recent(recent: list[dict]) -> str:
    lines = []
    for e in recent:
        bits = []
        if e.get("stories"):
            bits.append("; ".join(e["stories"]))
        if e.get("random_topic"):
            bits.append(f"random topic: {e['random_topic']}")
        if e.get("through_line"):
            bits.append(f"theme: {e['through_line']}")
        lines.append(f"- {e['date']}: " + " | ".join(bits))
    return "\n".join(lines) or "(nothing yet)"


def build_plan_prompt(today: dt.date, items: dict[str, list[dict]], recent: list[dict],
                      dutch_theme: str, dzogchen_theme: str) -> str:
    blocks = []
    for code, label in REGIONS.items():
        lines = [f"## {label} ({code})"]
        for idx, i in enumerate(items.get(code, []), 1):
            lines.append(f"[{code}{idx}] ({i['source']}) {i['title']} - {i['summary'][:200]}")
        blocks.append("\n".join(lines))
    n = STORIES_PER_REGION
    return f"""You are the editor of a daily two-host podcast mixing news and ideas. Date: {today.isoformat()}.
Today you only PLAN the episode: choose what goes in and find the one idea that ties it together. A scriptwriter works from your plan.

Ingredients of the episode:
- NEWS from four regions: CZ (Czechia), NL (Netherlands), EU (Europe), WORLD. Candidates are listed below.
- A RANDOM TOPIC that you choose.
- A DUTCH LESSON on the theme: "{dutch_theme}".
- A DZOGCHEN LESSON on the theme: "{dzogchen_theme}" (a practical lens on how the mind works).

Your tasks:
1. Pick exactly {n} stories per region (fewer only if fewer candidates exist). Rank by importance to ordinary people first (consequences, money, safety, politics, big shifts), variety second, fit with your through-line third. Never pick the same underlying event twice, even across regions. Skip trivia, gossip and routine sports results unless they say something bigger.
2. Mark exactly ONE pick per region as "deep" (most substance, best fit with the through-line). The rest are "quick".
3. Choose the RANDOM TOPIC: a surprising, true, well-documented story or phenomenon (history, science, nature, language, economics, a famous mystery) that you can explain accurately from general knowledge, with concrete details. It opens the episode as a story, so it must set up the through-line. Do not use any blocked random topic below.
4. Write the THROUGH-LINE: one plain sentence naming the idea that connects the random topic, the Dzogchen theme and several of the news stories (for example: "our minds invent dramatic stories to fill gaps in information"). Do not reuse a recent theme.
5. For every pick write an "angle": one short line on how it connects to the through-line or to another pick ("" if it does not).

BLOCKED (already covered in the last {HISTORY_DAYS} days). Do not pick these stories, random topics or themes. A listed story may return only if there is a clear new development:
{format_recent(recent)}

CANDIDATE NEWS ITEMS (IDs in brackets; items may be in Czech or Dutch):
{chr(10).join(blocks)}

OUTPUT: only valid JSON, no markdown fences:
{{
  "through_line": "one sentence",
  "random_topic": {{"title": "5-10 words", "why_it_fits": "one sentence"}},
  "picks": {{
    "CZ": [{{"id": "CZ3", "headline": "short English headline", "angle": "...", "depth": "deep"}}, {{"id": "CZ1", "headline": "...", "angle": "", "depth": "quick"}}],
    "NL": [...], "EU": [...], "WORLD": [...]
  }}
}}
"""


def normalize_plan(plan: dict, items: dict[str, list[dict]], by_id: dict[str, dict]) -> dict:
    """Repair/complete whatever the model returned so the rest of the pipeline can rely on it."""
    out = {"through_line": clean(str(plan.get("through_line") or ""), 300),
           "random_topic": "", "random_why": "", "picks": {}}
    rt = plan.get("random_topic")
    if isinstance(rt, dict):
        out["random_topic"] = clean(str(rt.get("title") or ""), 160)
        out["random_why"] = clean(str(rt.get("why_it_fits") or ""), 300)
    elif isinstance(rt, str):
        out["random_topic"] = clean(rt, 160)
    picks_raw = plan.get("picks") if isinstance(plan.get("picks"), dict) else {}
    for code in REGIONS:
        cands = [f"{code}{n}" for n in range(1, len(items.get(code, [])) + 1)]
        want = min(STORIES_PER_REGION, len(cands))
        chosen, seen = [], set()
        for p in picks_raw.get(code) or []:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            if pid in cands and pid not in seen:
                seen.add(pid)
                chosen.append({"id": pid,
                               "headline": clean(str(p.get("headline") or by_id[pid]["title"]), 160),
                               "angle": clean(str(p.get("angle") or ""), 200),
                               "depth": "deep" if p.get("depth") == "deep" else "quick"})
            if len(chosen) == want:
                break
        for pid in cands:                                   # top up if the model picked too few
            if len(chosen) >= want:
                break
            if pid not in seen:
                seen.add(pid)
                chosen.append({"id": pid, "headline": by_id[pid]["title"], "angle": "", "depth": "quick"})
        deep_seen = False
        for c in chosen:                                    # exactly one "deep" per region
            if c["depth"] == "deep" and not deep_seen:
                deep_seen = True
            else:
                c["depth"] = "quick"
        if chosen and not deep_seen:
            chosen[0]["depth"] = "deep"
        out["picks"][code] = chosen
    return out


def make_plan(client, prompt: str, items: dict, by_id: dict) -> dict:
    try:
        raw = extract_json(call_text_model(client, prompt, temperature=0.7, max_tokens=12000))
        plan = normalize_plan(raw, items, by_id)
        log(f"  through-line: {plan['through_line'] or '(none)'}")
        log(f"  random topic: {plan['random_topic'] or '(none)'}")
        return plan
    except Exception as ex:
        log(f"  planning failed ({str(ex)[:200]}); using a plain top-of-feed plan")
        return normalize_plan({}, items, by_id)


# --------------------------------------------------------------------------- stage 2: article text

def fetch_article(url: str, timeout: int = 15) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA_BROWSER,
                                                   "Accept-Language": "en,cs;q=0.8,nl;q=0.8"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(2_000_000)
            charset = r.headers.get_content_charset() or "utf-8"
        import trafilatura
        text = trafilatura.extract(raw.decode(charset, errors="replace"),
                                   include_comments=False, include_tables=False, favor_precision=True) or ""
        return re.sub(r"\s+", " ", text).strip()[:ARTICLE_CHARS]
    except Exception as ex:
        log(f"  article fetch failed {url[:70]}: {str(ex)[:80]}")
        return ""


def fetch_articles(plan: dict, by_id: dict[str, dict]) -> dict[str, str]:
    ids = [p["id"] for lst in plan["picks"].values() for p in lst]
    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = list(pool.map(lambda pid: fetch_article(by_id[pid]["link"]) if by_id[pid]["link"] else "", ids))
    out, full = {}, 0
    for pid, text in zip(ids, texts):
        summary = by_id[pid]["summary"]
        if len(text) >= 400 and len(text) > len(summary):
            out[pid] = text
            full += 1
        else:
            out[pid] = summary
    log(f"  full article text for {full}/{len(ids)} stories (rest use the RSS summary)")
    return out


# --------------------------------------------------------------------------- stage 3: write the script

TONE_REFERENCE = """Anna: Have you ever noticed how, like, completely exhausting it is to live inside your own head sometimes?
Ben: Oh, yeah. Constantly.
Anna: Like, you're lying in bed, trying to sleep, and suddenly your brain decides to write a five-act tragedy about an email you sent three days ago.
Ben: Or you're spinning a massive conspiracy theory about why a coworker looked at you funny in the hallway.
Anna: Okay, I like this. So my anxious brain is essentially TikTok.
Ben: Essentially, yes. The algorithm's only goal is to keep you engaged, so it throws provocative content at you, like "Did I leave the stove on?"
Anna: Now, I want to push back on this a little. Telling me the essence of my mind is emptiness sounds, well, kind of depressing. Like a sensory deprivation tank. How is that a good thing?
Ben: Great question, because "empty" here doesn't mean dead. Think of a computer's RAM before you open any apps. It's clear, ready, and nothing is lagging."""


def build_write_prompt(today: dt.date, plan: dict, by_id: dict[str, dict], articles: dict[str, str],
                       recent: list[dict], dutch_theme: str, dzogchen_theme: str, dutch_level: str) -> str:
    T = TARGET_WORDS
    w = {k: int(T * v) for k, v in
         dict(hook=0.05, random=0.12, news=0.45, dutch=0.12, dzogchen=0.18, close=0.05).items()}
    blocks = []
    for code, label in REGIONS.items():
        lst = plan["picks"].get(code, [])
        lines = [f"## {label} ({code}): {len(lst)} stories"]
        for p in lst:
            i = by_id[p["id"]]
            lines.append(f"[{p['id']}] {p['depth'].upper()} ({i['source']}) {p['headline']}")
            if p["angle"]:
                lines.append(f"  Connection: {p['angle']}")
            lines.append(f"  Original title: {i['title']}")
            lines.append(f"  Text: {articles.get(p['id']) or i['summary']}")
        blocks.append("\n".join(lines))

    if plan["through_line"]:
        theme = (f"THROUGH-LINE (the idea tying today together): {plan['through_line']}\n"
                 f"RANDOM TOPIC (opens the episode as a story): {plan['random_topic']}"
                 + (f" ({plan['random_why']})" if plan["random_why"] else ""))
    else:
        theme = ("No plan was supplied. First choose a through-line yourself: one idea connecting the news, "
                 "the Dzogchen theme and a surprising true random topic of your choice (a story or mystery with "
                 f"concrete details). Do NOT reuse these recent topics/themes:\n{format_recent(recent)}")

    return f"""You write the script for a daily two-host podcast. Date: {today.isoformat()}. Output is spoken British English.

{theme}

HOSTS
- Anna: the listener's stand-in. Curious, quick, a bit sceptical. Asks "wait, what does that mean?", pushes back ("I want to push back on that a little"), makes most of the jokes, reacts honestly.
- Ben: the explainer. Knows the facts, gives dates, numbers and concrete details, explains everything with everyday analogies (tech, kitchen, traffic, apps), dry humour. Explains any jargon in half a sentence.

STYLE (this matters more than anything)
- Spoken British English: British vocabulary, idiom and humour (understatement, dry irony, gentle teasing). Natural, never a cartoon: no "cheerio", no forced slang. Avoid Americanisms ("gotten", "awesome", "you guys").
- Smart but easy: concrete details beat generalities. Playful, lightly joking now and then (about one joke per 150 words); jokes come from observation, never from mocking victims or tragedies.
- Running banter between the two: Anna teases Ben for being the know-it-all, Ben answers with dry understatement. Each part of the show (including both lessons) has at least one small joke exchange, and at least two callbacks to an earlier joke or story. Keep every joke light and short; the information still comes first.
- Conversation, not a list. Many very short turns ("Right." "Oh, wow." "Exactly.") as their own turns, occasional interruptions, rhetorical questions before explanations, direct address to the listener now and then ("for you listening").
- Every transition says WHY the next item follows (contrast, echo, cause, irony). Never "next story" or "moving on". Call back to earlier items later in the episode.
- No "welcome to another exciting episode" filler. Say "welcome to today's briefing" once, at most.
- Tone reference (do NOT reuse its content or wording):
{TONE_REFERENCE}

LENGTH: about {T} words of spoken text in total, which is a HARD MINIMUM. A short script is a failure. If you run low, deepen the explanations, analogies and banter instead of wrapping up early.

RUN OF SHOW (budgets in words)
1. HOOK (~{w['hook']}): Anna opens with a relatable, funny observation that sets up the through-line, then a one-breath promise of today's route. Straight in.
2. RANDOM TOPIC (~{w['random']}): tell it as a mini-mystery or story with concrete details; Ben explains the mechanism with an analogy; end by stating the through-line in plain words. Use only well-established facts; no exact statistics you are unsure of.
3. NEWS (~{w['news']}): regions in order CZ, NL, EU, WORLD, each opened with a natural transition. For each region: the DEEP story gets ~150-200 words (what happened, background, why it matters, one concrete detail, and its connection to the through-line); the QUICK stories get 2-3 sentences each, grouped as a rapid-fire run, with Anna asking at least one pushback question per region. Cover ALL listed stories ({sum(len(v) for v in plan['picks'].values())} total). Mention the source by name now and then. Use ONLY facts in the supplied text; if a story only has a headline, keep it short and don't invent details. Source text may be Czech or Dutch: translate to English.
4. DUTCH LESSON (~{w['dutch']}): theme "{dutch_theme}", level {dutch_level}. Open with a playful link to the through-line. Teach 6 phrases: Dutch phrase, meaning, an English-speaker pronunciation tip, one example sentence. Anna plays the learner who tries to say them, Ben corrects lightly. Wrap EVERY Dutch word or phrase in [nl]...[/nl], e.g. [nl]Goedemorgen[/nl].
5. DZOGCHEN LESSON (~{w['dzogchen']}): theme "{dzogchen_theme}". This is the silver lining of the episode: a lens that re-reads the day's stories. Anna asks the jargon or sceptic question, Ben explains with one modern analogy, at least two callbacks to stories heard earlier, and one tiny practical thing to try today (30 seconds). Stick to widely taught, non-sectarian points; do NOT invent quotes or attribute quotes to named teachers (say "teachers in the Dzogchen tradition say"). Explain Tibetan terms when used. Set "style" to "calm, slower, warm" on these turns.
6. CLOSE (~{w['close']}): one-breath recap of the route ("we went from X to Y to Z"), one sentence of takeaway for the listener, a dry sign-off joke. No meditation segment.

STORIES (IDs in brackets):
{chr(10).join(blocks)}

OUTPUT: only valid JSON, no markdown fences:
{{
  "title": "catchy episode title, max 80 chars, no date",
  "dutch_phrases": [{{"nl": "the Dutch phrase, plain text", "en": "English meaning"}}, ... the 6 phrases taught in the Dutch lesson],
  "turns": [{{"speaker": "Anna", "section": "hook", "text": "spoken words only, no stage directions, no markdown", "style": "optional, max 6 words, e.g. amused / mock-serious / calm, slower, warm"}}, ...]
}}
"section" goes ONLY on the first turn of each part of the show, with one of: "hook", "random", "news", "dutch", "dzogchen", "close".
"speaker" must be exactly "Anna" or "Ben". Write numbers and abbreviations the way they are spoken.
"""


SPEAKER_ALIASES = {"anna": "Anna", "host a": "Anna", "a": "Anna", "ben": "Ben", "host b": "Ben", "b": "Ben"}


def validate_script(data: dict) -> None:
    turns = data.get("turns")
    if not isinstance(turns, list):
        raise ValueError("no turns list")
    good = []
    for t in turns:
        if not isinstance(t, dict) or not isinstance(t.get("text"), str) or not t["text"].strip():
            continue
        sp = SPEAKER_ALIASES.get(str(t.get("speaker", "")).strip().lower())
        if sp not in HOSTS:
            continue
        turn = {"speaker": sp, "text": t["text"].strip()}
        if isinstance(t.get("style"), str) and t["style"].strip():
            turn["style"] = t["style"].strip()[:60]
        if t.get("section") in SECTIONS:
            turn["section"] = t["section"]
        good.append(turn)
    bad = len(turns) - len(good)
    if len(good) < 10 or bad > max(2, int(0.05 * len(turns))):
        raise ValueError(f"script too broken: {len(good)} usable turns, {bad} bad")
    data["turns"] = good
    data["title"] = clean(str(data.get("title") or "Daily Briefing"), 80)
    phrases = []
    for ph in data.get("dutch_phrases") or []:
        if isinstance(ph, dict) and str(ph.get("nl") or "").strip() and str(ph.get("en") or "").strip():
            phrases.append({"nl": clean(str(ph["nl"]), 120), "en": clean(str(ph["en"]), 160)})
    data["dutch_phrases"] = phrases[:10]


def word_count(data: dict) -> int:
    return sum(len(re.sub(r"\[/?nl\]", "", t["text"]).split()) for t in data["turns"])


def generate_script(client, prompt: str) -> dict:
    best = None
    for attempt in range(3):
        try:
            data = extract_json(call_text_model(client, prompt))
            validate_script(data)
        except Exception as ex:
            log(f"  script attempt {attempt + 1} invalid: {str(ex)[:200]}")
            continue
        words = word_count(data)
        log(f"  script attempt {attempt + 1}: {words} words")
        if best is None or words > word_count(best):
            best = data
        if words >= TARGET_WORDS * MIN_RATIO:
            return data
        prompt += (f"\n\nYour previous draft had only {words} words. Write at least {TARGET_WORDS} words: "
                   "more depth per story, longer analogies, more banter, fuller lessons. Same JSON format.")
    if best is None:
        raise RuntimeError("could not get a valid script from the model")
    return best


# --------------------------------------------------------------------------- stage 4: fact-check

def label_turns(turns: list[dict]) -> list[str]:
    labels, cur = [], "hook"
    for t in turns:
        cur = t.get("section") or cur
        labels.append(cur)
    return labels


def build_fact_check_prompt(turns: list[dict], labels: list[str], plan: dict, by_id: dict[str, dict],
                            articles: dict[str, str]) -> str | None:
    sources = []
    for lst in plan["picks"].values():
        for p in lst:
            sources.append(f"[{p['id']}] {p['headline']}\n{articles.get(p['id']) or by_id[p['id']]['summary']}")
    lines = []
    for i, (t, lab) in enumerate(zip(turns, labels)):
        kind = "NEWS" if lab == "news" else "GENERAL" if lab in ("random", "dzogchen") else None
        if kind:
            lines.append(f"[{i}] ({kind}) {t['speaker']}: {t['text']}")
    if not any("(NEWS)" in l for l in lines):
        return None
    return f"""You are the fact-checker for a podcast script. Two hosts, Anna and Ben, talk in a relaxed, witty way. You check only the lines below; each has an index in square brackets.

NEWS lines must be supported by the SOURCES. For each NEWS line, check every concrete claim: names, numbers, dates, places, quotes, causes, outcomes. If a claim is not supported by the sources, or contradicts them, rewrite that line so it only says what the sources support: remove or soften the claim. Never add facts that are not in the sources.

GENERAL lines (the random topic and the Dzogchen lesson) cannot be checked against the sources. Only fix claims that are likely wrong or too precise: exact statistics, precise dates unless famous, quotes attributed to a named person or teacher, and superlatives ("first", "only", "record"). Replace them with safer wording. Do not touch anything else.

Rules for every rewrite: keep the same speaker's voice, the jokes and the banter, roughly the same length, British English. Do not touch lines that are fine.

SOURCES:
{chr(10).join(sources)}

LINES TO CHECK:
{chr(10).join(lines)}

OUTPUT: only valid JSON, no markdown fences. Include ONLY lines that need a change:
{{"fixes": [{{"turn": 12, "text": "the corrected line, spoken words only", "reason": "max 12 words"}}]}}
If everything is fine return {{"fixes": []}}.
"""


def apply_fixes(turns: list[dict], labels: list[str], fixes) -> list[dict]:
    applied = []
    if not isinstance(fixes, list):
        return applied
    for f in fixes:
        try:
            i = int(f["turn"])
            text = str(f["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= i < len(turns)) or labels[i] not in ("news", "random", "dzogchen") or not text:
            continue
        old_words = len(turns[i]["text"].split())
        new_words = len(text.split())
        if new_words < 3 or new_words > old_words * 1.6 + 10:
            continue
        turns[i]["text"] = text
        applied.append({"turn": i, "section": labels[i], "reason": clean(str(f.get("reason") or ""), 120)})
    return applied


def fact_check(client, script: dict, plan: dict, by_id: dict[str, dict],
               articles: dict[str, str]) -> tuple[dict, list[dict]]:
    """Never blocks the episode: any failure leaves the script as it was."""
    try:
        labels = label_turns(script["turns"])
        prompt = build_fact_check_prompt(script["turns"], labels, plan, by_id, articles)
        if prompt is None:
            log("  fact-check skipped: no news section markers in the script")
            return script, []
        raw = extract_json(call_text_model(client, prompt, temperature=0.2, max_tokens=16000))
        applied = apply_fixes(script["turns"], labels, raw.get("fixes"))
        log(f"  fact-check: {len(applied)} line(s) corrected")
        for a in applied[:15]:
            log(f"    turn {a['turn']} ({a['section']}): {a['reason']}")
        return script, applied
    except Exception as ex:
        log(f"  fact-check failed ({str(ex)[:200]}); keeping the script unchanged")
        return script, []


# --------------------------------------------------------------------------- audio

def strip_tags(text: str) -> str:
    return re.sub(r"\[/?nl\]", "", text)


def split_lang(text: str) -> list[tuple[str, str]]:
    parts = []
    for chunk in re.split(r"(\[nl\].*?\[/nl\])", text, flags=re.S):
        if not re.search(r"\w", chunk):
            continue
        if chunk.startswith("[nl]"):
            parts.append(("nl", strip_tags(chunk).strip()))
        else:
            parts.append(("en", strip_tags(chunk).strip()))
    return parts


def chunk_turns(turns: list[dict], limit: int = TTS_CHUNK_CHARS) -> list[list[dict]]:
    chunks, cur, size = [], [], 0
    for t in turns:
        n = len(t["text"]) + 8
        if cur and (size + n > limit or t.get("section") in BREAK_SECTIONS):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def turn_style(t: dict) -> str:
    base = t.get("style") or DEFAULT_STYLE
    base = f"{base}, {ACCENT}" if ACCENT else base
    if t.get("section") == "dutch" or t.get("dutch"):
        return base + "; say every Dutch word and phrase with authentic native Dutch pronunciation, not English"
    return base + "; pronounce Czech and Dutch names and places the way a native speaker would"


def make_silence_wav(workdir: Path, seconds: float = SECTION_PAUSE) -> Path:
    path = workdir / "break.wav"
    if not path.exists():
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\0\0" * int(24000 * seconds))
    return path


class ModelUnavailable(RuntimeError):
    """This TTS model cannot be used any more today (retired name, or its daily quota is used up)."""


def quota_info(msg: str) -> dict:
    """Read a 429 error: is it a per-day limit, and how long does Google say to wait?"""
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", msg)
    return {"daily": bool(re.search(r"PerDay|per_day|requests_per_day", msg, re.I)),
            "delay": float(m.group(1)) if m else None}


def tts_gemini(client, turns: list[dict], workdir: Path, model: str | None = None) -> list[Path]:
    """Synthesize the whole episode with ONE model, so an episode never mixes voices."""
    from google.genai import types
    model = model or TTS_MODELS[0]
    voices = {"Anna": os.getenv("ANNA_VOICE", "Kore"), "Ben": os.getenv("BEN_VOICE", "Charon")}   # Charon = deeper male voice
    speech = types.SpeechConfig(multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
        speaker_voice_configs=[
            types.SpeakerVoiceConfig(speaker=name, voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=v)))
            for name, v in voices.items()]))
    chunks = chunk_turns(turns)
    paths: list[Path] = []
    for n, chunk in enumerate(chunks, 1):
        # Newer TTS models need one text part per turn, each tagged with its speaker.
        parts = [{"text": strip_tags(t["text"]),
                  "speech_metadata": {"speaker": t["speaker"], "style": turn_style(t)}}
                 for t in chunk]
        contents = [{"role": "user", "parts": parts}]
        data = None
        for attempt in range(1, TTS_ATTEMPTS + 1):
            wait = 10.0
            try:
                resp = client.models.generate_content(
                    model=model, contents=contents,
                    config=types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=speech))
                data = resp.candidates[0].content.parts[0].inline_data.data
                if data:
                    break
                log(f"  TTS chunk {n}/{len(chunks)} attempt {attempt} on {model}: empty audio")
            except Exception as ex:
                msg = str(ex)
                log(f"  TTS chunk {n}/{len(chunks)} attempt {attempt} on {model}: {msg[:300]}")
                if "NOT_FOUND" in msg or "404" in msg:
                    raise ModelUnavailable(f"{model} not found")
                if "INVALID_ARGUMENT" in msg or "PERMISSION_DENIED" in msg:
                    raise RuntimeError(f"Gemini TTS rejected the request on chunk {n}")   # permanent
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    q = quota_info(msg)
                    if q["daily"]:
                        raise ModelUnavailable(f"{model} daily quota used up")
                    wait = min((q["delay"] + 3) if q["delay"] else 30.0 * attempt, 150.0)
                else:
                    wait = 20.0 * attempt
            if attempt < TTS_ATTEMPTS:
                log(f"  waiting {wait:.0f}s before retrying chunk {n}")
                time.sleep(wait)
        if not data:
            raise RuntimeError(f"Gemini TTS failed on chunk {n} ({model})")
        wav = workdir / f"chunk_{n:03d}.wav"
        if data[:4] == b"RIFF":              # newer models return a complete WAV file
            wav.write_bytes(data)
        else:                                # older models return raw 24 kHz 16-bit mono PCM
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(data)
        if paths and chunk[0].get("section") in BREAK_SECTIONS:
            paths.append(make_silence_wav(workdir))
        paths.append(wav)
        log(f"  TTS chunk {n}/{len(chunks)} ok")
        if n < len(chunks):
            time.sleep(TTS_PAUSE_SECONDS)
    return paths


EDGE_VOICES = {("Anna", "en"): "en-GB-SoniaNeural", ("Ben", "en"): "en-GB-RyanNeural",
               ("Anna", "nl"): "nl-NL-ColetteNeural", ("Ben", "nl"): "nl-NL-MaartenNeural"}


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr[-500:]}")


def tts_edge(turns: list[dict], workdir: Path) -> list[Path]:
    import edge_tts

    silence = workdir / "silence.mp3"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.35",
         "-c:a", "libmp3lame", "-b:a", "48k", "-ar", "24000", "-ac", "1", str(silence)])

    section_break = workdir / "break.mp3"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", str(SECTION_PAUSE),
         "-c:a", "libmp3lame", "-b:a", "48k", "-ar", "24000", "-ac", "1", str(section_break)])

    jobs: list[tuple[Path, str, str]] = []
    order: list[Path] = []
    for ti, t in enumerate(turns):
        if ti and t.get("section") in BREAK_SECTIONS:
            order.append(section_break)
        for pi, (lang, text) in enumerate(split_lang(t["text"])):
            p = workdir / f"seg_{ti:04d}_{pi:02d}.mp3"
            jobs.append((p, EDGE_VOICES[(t["speaker"], lang)], text))
            order.append(p)
        order.append(silence)

    async def go():
        sem = asyncio.Semaphore(4)

        async def one(p, voice, text):
            async with sem:
                for attempt in range(4):
                    try:
                        await edge_tts.Communicate(text, voice).save(str(p))
                        return
                    except Exception as ex:
                        if attempt == 3:
                            raise
                        log(f"  edge retry {p.name}: {str(ex)[:100]}")
                        await asyncio.sleep(2 * (attempt + 1))
        await asyncio.gather(*(one(*j) for j in jobs))

    asyncio.run(go())
    return order


def to_mp3(parts: list[Path], out: Path) -> None:
    lst = out.parent / "concat.txt"
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ac", "1", "-ar", "24000",
         "-c:a", "libmp3lame", "-b:a", "64k", str(out)])


def duration_seconds(path: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
        return float(r.stdout.strip())
    except (FileNotFoundError, ValueError):
        pass
    try:                                           # no ffprobe (static ffmpeg build): read it from ffmpeg
        r = subprocess.run(["ffmpeg", "-i", str(path)], capture_output=True, text=True)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
        if m:
            return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    except FileNotFoundError:
        pass
    return path.stat().st_size / (64000 / 8)


def make_audio(client, turns: list[dict], out: Path) -> str:
    """Try each Gemini TTS model in order, each from scratch (one voice per episode), then Edge TTS.
    Returns the engine used: a Gemini model name, or "edge"."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        parts, engine = None, "edge"
        if client is not None:
            for model in TTS_MODELS:
                sub = work / re.sub(r"[^A-Za-z0-9.-]", "_", model)
                sub.mkdir()
                try:
                    parts = tts_gemini(client, turns, sub, model)
                    engine = model
                    break
                except Exception as ex:
                    log(f"Gemini TTS model {model} unusable ({str(ex)[:200]}); trying the next option")
        if parts is None:
            log("All Gemini TTS models failed; falling back to Edge TTS")
            parts = tts_edge(turns, work)
        to_mp3(parts, out)
    return engine


# --------------------------------------------------------------------------- publishing

def prune(root: Path, state: dict, today: dt.date) -> None:
    ep_dir = root / "episodes"
    for e in state["episodes"]:
        age = (today - dt.date.fromisoformat(e["date"])).days
        if e.get("file") and age >= KEEP_DAYS:
            (ep_dir / e["file"]).unlink(missing_ok=True)
            e["file"] = None
    keep = {e["file"] for e in state["episodes"] if e.get("file")}
    for f in ep_dir.glob("*.mp3"):
        if f.name not in keep:
            f.unlink()
    state["episodes"] = sorted(state["episodes"], key=lambda e: e["date"])[-60:]


def render_notes(e: dict) -> str:
    """Episode description as simple HTML (AntennaPod renders links, lists and bold)."""
    from html import escape as h
    d = e.get("notes_data")
    if not d:
        return "<p>" + h(e.get("notes", "")).replace("\n", "<br/>") + "</p>"
    out = []
    if d.get("through_line"):
        out.append(f"<p><b>Today's thread:</b> {h(d['through_line'])}</p>")
    if d.get("random_topic"):
        out.append(f"<p><b>Random topic:</b> {h(d['random_topic'])}</p>")
    out.append("<p><b>News</b></p>")
    region = None
    for n in d.get("news", []):
        if n["region"] != region:
            if region is not None:
                out.append("</ul>")
            region = n["region"]
            out.append(f"<p><i>{h(region)}</i></p><ul>")
        link = n.get("link") or ""
        title = h(n["headline"])
        src = f" ({h(n['source'])})" if n.get("source") else ""
        out.append(f"<li><a href=\"{h(link, quote=True)}\">{title}</a>{src}</li>" if link else f"<li>{title}{src}</li>")
    if region is not None:
        out.append("</ul>")
    out.append(f"<p><b>Dutch: {h(d.get('dutch_theme', ''))}</b></p>")
    if d.get("dutch_phrases"):
        out.append("<ul>" + "".join(f"<li>{h(p['nl'])} = {h(p['en'])}</li>" for p in d["dutch_phrases"]) + "</ul>")
    out.append(f"<p><b>Dzogchen:</b> {h(d.get('dzogchen_theme', ''))}</p>")
    return "".join(out)


def build_feed(root: Path, base: str, feed_dir: str, state: dict) -> None:
    chan_url = f"{base}/{feed_dir}"
    items = []
    for e in sorted(state["episodes"], key=lambda e: e["date"], reverse=True):
        if not e.get("file") or not (root / "episodes" / e["file"]).exists():
            continue
        pub = dt.datetime.fromisoformat(e["published"])
        items.append(
            "<item>"
            f"<title>{escape(e['title'])}</title>"
            f"<description><![CDATA[{render_notes(e).replace(']]>', ']] >')}]]></description>"
            f"<pubDate>{email.utils.format_datetime(pub)}</pubDate>"
            f"<guid isPermaLink=\"false\">{escape(e['date'])}-daily-briefing</guid>"
            f"<enclosure url=\"{escape(chan_url)}/episodes/{escape(e['file'])}\" "
            f"length=\"{int(e['size'])}\" type=\"audio/mpeg\"/>"
            f"<itunes:duration>{int(e['duration'])}</itunes:duration>"
            "<itunes:explicit>false</itunes:explicit>"
            "</item>")
    xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
           "<rss version=\"2.0\" xmlns:itunes=\"http://www.itunes.com/dtds/podcast-1.0.dtd\">\n<channel>\n"
           "<title>Daily Briefing</title>\n"
           f"<link>{escape(chan_url)}/</link>\n"
           "<language>en</language>\n"
           "<description>Czech, Dutch, European and world news, a random topic, a Dutch lesson and a Dzogchen lesson.</description>\n"
           "<itunes:author>Daily Briefing</itunes:author>\n"
           "<itunes:explicit>false</itunes:explicit>\n"
           "<itunes:category text=\"News\"/>\n" +
           "\n".join(items) + "\n</channel>\n</rss>\n")
    (root / "feed.xml").write_text(xml, encoding="utf-8")


def make_notes(script: dict, dutch: str, dzogchen: str) -> str:
    lines = [f"Theme: {script['through_line']}"] if script.get("through_line") else []
    lines += ["News:"] + [f"- {s}" for s in script["covered_stories"]]
    if script["random_topic"]:
        lines.append(f"Random topic: {script['random_topic']}")
    lines += [f"Dutch: {dutch}", f"Dzogchen: {dzogchen}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="site")
    ap.add_argument("--dry-run", action="store_true", help="fetch feeds and print the planning prompt only")
    ap.add_argument("--no-audio", action="store_true",
                    help="plan + write the script only (writes script.txt and plan.json); no state changes")
    ap.add_argument("--force", action="store_true", default=bool(os.getenv("FORCE")))
    args = ap.parse_args()

    now = dt.datetime.now(TZ)
    today = now.date()
    feed_dir = os.getenv("FEED_DIR", "podcast")
    base = os.getenv("SITE_URL", "http://localhost:8000").rstrip("/")
    site = Path(args.site)
    root = site / feed_dir
    (root / "episodes").mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    state = load_state(state_path)
    curriculum = json.loads((HERE / "curriculum.json").read_text(encoding="utf-8"))
    feeds = json.loads((HERE / "feeds.json").read_text(encoding="utf-8"))

    existing = next((e for e in state["episodes"] if e["date"] == today.isoformat()), None)
    if existing and existing.get("file") and not args.force and not args.dry_run and not args.no_audio:
        log(f"Episode for {today} already exists; use --force to regenerate.")
        build_feed(root, base, feed_dir, state)
        return 0

    recent = recent_episodes(state, today)
    seen_links = {l for e in recent for l in e.get("links", [])}
    seen_titles = {norm(t) for e in recent for t in e.get("item_titles", [])}

    items: dict[str, list[dict]] = {}
    for code, urls in feeds.items():
        log(f"{code}:")
        items[code] = pick_items(fetch_region(urls), now.astimezone(UTC), seen_links, seen_titles)
        log(f"  -> {len(items[code])} candidates")
        if len(items[code]) < STORIES_PER_REGION:
            log(f"  WARNING: only {len(items[code])} candidates for {code}")
    if not any(items.values()):
        log("No news items fetched at all; aborting.")
        return 1
    by_id = {f"{code}{n}": i for code, lst in items.items() for n, i in enumerate(lst, 1)}

    if existing:                                  # forced re-run keeps the same lesson themes
        d_idx, z_idx = existing["dutch_idx"], existing["dzogchen_idx"]
    else:
        d_idx = state["dutch_next"] % len(curriculum["dutch"])
        z_idx = state["dzogchen_next"] % len(curriculum["dzogchen"])
    dutch_theme, dzogchen_theme = curriculum["dutch"][d_idx], curriculum["dzogchen"][z_idx]

    plan_prompt = build_plan_prompt(today, items, recent, dutch_theme, dzogchen_theme)
    if args.dry_run:
        print(plan_prompt)
        return 0

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log("GEMINI_API_KEY is not set.")
        return 1
    from google import genai
    from google.genai import types as genai_types
    # a hung request must fail fast and be retried, not eat the 60-minute limit
    client = genai.Client(api_key=api_key, http_options=genai_types.HttpOptions(
        timeout=int(os.getenv("API_TIMEOUT_SECONDS", "300")) * 1000))

    stamp("Planning episode...")
    plan = make_plan(client, plan_prompt, items, by_id)
    stamp("Fetching article text...")
    articles = fetch_articles(plan, by_id)
    stamp("Writing script...")
    write_prompt = build_write_prompt(today, plan, by_id, articles, recent,
                                      dutch_theme, dzogchen_theme, curriculum["dutch_level"])
    script = generate_script(client, write_prompt)
    fixes: list[dict] = []
    if FACT_CHECK:
        stamp("Fact-checking script...")
        script, fixes = fact_check(client, script, plan, by_id, articles)

    if args.no_audio:
        text = "\n\n".join(f"{t['speaker']}: {t['text']}" for t in script["turns"])
        Path("script.txt").write_text(text, encoding="utf-8")
        Path("plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        print(text)
        log(f"\n{word_count(script)} words (~{word_count(script) / 150:.0f} min) -> script.txt, plan.json")
        return 0

    fname = f"{today.isoformat()}.mp3"
    out = root / "episodes" / fname
    stamp("Generating audio...")
    engine = make_audio(client, script["turns"], out)
    log(f"  audio engine: {engine}")
    stamp("audio finished")
    title = script["title"]
    if engine == "edge":
        title += " [backup voices]"
        print("::warning title=Backup voices::Gemini TTS was unavailable, so this episode uses the flatter Edge voices.", flush=True)
    elif engine != TTS_MODELS[0]:
        title += " [alt voices]"
        print(f"::warning title=Alternative voice model::{TTS_MODELS[0]} was unavailable; this episode used {engine}.", flush=True)

    picked = [p for lst in plan["picks"].values() for p in lst]
    used = [by_id[p["id"]] for p in picked]
    stories = [p["headline"] for p in picked]
    news = [{"region": REGIONS[code], "headline": p["headline"], "link": by_id[p["id"]]["link"],
             "source": by_id[p["id"]]["source"]} for code, lst in plan["picks"].items() for p in lst]
    meta = {"covered_stories": stories, "random_topic": plan["random_topic"], "through_line": plan["through_line"]}
    entry = {
        "date": today.isoformat(), "title": title, "file": fname,
        "published": now.isoformat(), "size": out.stat().st_size, "duration": duration_seconds(out),
        "stories": stories, "random_topic": plan["random_topic"], "through_line": plan["through_line"],
        "links": [i["link"] for i in used], "item_titles": [i["title"] for i in used],
        "dutch_idx": d_idx, "dzogchen_idx": z_idx, "fact_check": fixes,
        "notes": make_notes(meta, dutch_theme, dzogchen_theme), "engine": engine,
        "notes_data": {"through_line": plan["through_line"], "random_topic": plan["random_topic"], "news": news,
                       "dutch_theme": dutch_theme, "dutch_phrases": script.get("dutch_phrases", []),
                       "dzogchen_theme": dzogchen_theme},
    }
    state["episodes"] = [e for e in state["episodes"] if e["date"] != today.isoformat()] + [entry]
    if not existing:
        state["dutch_next"] = d_idx + 1
        state["dzogchen_next"] = z_idx + 1
    prune(root, state, today)
    save_state(state_path, state)
    build_feed(root, base, feed_dir, state)
    log(f"Done: {fname}, {entry['duration'] / 60:.1f} min, {entry['size'] / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())

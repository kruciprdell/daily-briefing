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
import time
import wave
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import feedparser

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Amsterdam")
UTC = dt.timezone.utc
UA = "Mozilla/5.0 (compatible; daily-podcast-bot/1.0)"

# Model names churn. Each list is tried in order; override with a comma-separated env var.
TEXT_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_TEXT_MODELS", "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite").split(",") if m.strip()]
TTS_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_TTS_MODELS", "gemini-3.8-flash-tts,gemini-3.8-flash-lite-tts,gemini-3.1-flash-tts-preview").split(",") if m.strip()]

KEEP_DAYS = int(os.getenv("KEEP_DAYS", "14"))       # mp3 files older than this are deleted
HISTORY_DAYS = 7                                    # topics from the last N days are blocked
STORIES_PER_REGION = int(os.getenv("STORIES_PER_REGION", "5"))
ITEMS_PER_REGION = 12                               # candidates handed to the model per region
MAX_AGE_HOURS = 36
TARGET_WORDS = int(os.getenv("TARGET_WORDS", "3000"))   # ~150 wpm -> ~20 min
TTS_CHUNK_CHARS = 2800
TTS_PAUSE_SECONDS = int(os.getenv("TTS_PAUSE", "22"))   # free-tier TTS has a low requests/min cap
HOSTS = ("Anna", "Ben")
REGIONS = {"CZ": "Czechia", "NL": "Netherlands", "EU": "Europe", "WORLD": "World"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


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


# --------------------------------------------------------------------------- script generation

def build_prompt(today: dt.date, items: dict[str, list[dict]], recent: list[dict],
                 dutch_theme: str, dzogchen_theme: str, dutch_level: str) -> str:
    news_words = int(TARGET_WORDS * 0.55)
    n = STORIES_PER_REGION
    blocks = []
    for code, label in REGIONS.items():
        lines = [f"## {label}"]
        for idx, i in enumerate(items.get(code, []), 1):
            lines.append(f"[{code}{idx}] ({i['source']}) {i['title']} - {i['summary']}")
        blocks.append("\n".join(lines))
    covered = []
    for e in recent:
        covered.append(f"- {e['date']}: " + "; ".join(e.get("stories", [])) +
                       (f"; random topic: {e['random_topic']}" if e.get("random_topic") else ""))
    covered_txt = "\n".join(covered) or "(nothing yet)"
    random_blocked = "; ".join(e["random_topic"] for e in recent if e.get("random_topic")) or "(none)"

    return f"""You write the script for a daily two-host podcast. Date: {today.isoformat()}.
Hosts: Anna and Ben. Casual, quick, witty, a little dark and absurdist (Central European humour), never mocking victims of tragedies. They talk in plain English, interrupt each other, link stories where a REAL connection exists, and explain jargon in half a sentence. No filler like "welcome to another exciting episode".

TOTAL LENGTH: about {TARGET_WORDS} words of spoken text (about 20 minutes). Follow this order and budget:
1. Short cold open (~60 words).
2. NEWS (~{news_words} words): exactly {n} stories each from Czechia, Netherlands, Europe, World ({n * 4} total), picked ONLY from the candidate items below, most important first within each region. Per story: what happened, why it matters, one concrete detail. Source items may be in Czech or Dutch: translate to English. Do not invent facts beyond the item text; if an item is thin, say what is known and stay brief.
3. RANDOM TOPIC (~{int(TARGET_WORDS * 0.10)} words): one surprising true fact or idea (science, history, nature, language, economics...). It may loosely connect to the news but need not. Must NOT be any of these recent topics: {random_blocked}.
4. DUTCH LESSON (~{int(TARGET_WORDS * 0.15)} words): theme "{dutch_theme}", level {dutch_level}. Teach 6 phrases. For each: the Dutch phrase, meaning, an English-speaker pronunciation tip, one example sentence. Hosts practise lines back and forth. Wrap EVERY Dutch word or phrase in [nl]...[/nl] markers, e.g. [nl]Goedemorgen[/nl].
5. DZOGCHEN LESSON (~{int(TARGET_WORDS * 0.15)} words): theme "{dzogchen_theme}". Practical, plain English, one everyday example. Stick to widely taught, non-sectarian points. Do NOT invent quotes and do NOT attribute specific quotes to named teachers; say "teachers in the Dzogchen tradition say..." when needed. Use Tibetan terms sparingly and explain them. Calm, slightly slower tone.
6. CLOSE (~40 words): one-sentence recap of the Dzogchen idea, goodbye. No meditation segment.

BLOCKED (already covered in the last {HISTORY_DAYS} days). Skip these stories. A listed story may return only if there is a clear NEW development, and then in one sentence:
{covered_txt}

CANDIDATE NEWS ITEMS (IDs in brackets):
{chr(10).join(blocks)}

OUTPUT: return ONLY valid JSON, no markdown fences, with this shape:
{{
  "title": "catchy episode title, max 80 chars, no date",
  "turns": [{{"speaker": "Anna" or "Ben", "text": "spoken text, no stage directions, no markdown"}}, ...],
  "used_ids": ["CZ1", "NL3", ...],
  "covered_stories": ["short English one-line headline per story covered", ...],
  "random_topic": "5-10 word description of the random topic"
}}
Turns should be 1-5 sentences each. Numbers and abbreviations written the way they are spoken.
"""


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


def validate_script(data: dict) -> None:
    if not isinstance(data.get("turns"), list) or len(data["turns"]) < 10:
        raise ValueError("script has too few turns")
    for t in data["turns"]:
        if t.get("speaker") not in HOSTS or not isinstance(t.get("text"), str) or not t["text"].strip():
            raise ValueError(f"bad turn: {t!r}")
    data.setdefault("used_ids", [])
    data.setdefault("covered_stories", [])
    data.setdefault("random_topic", "")
    data.setdefault("title", "Daily Briefing")


def word_count(data: dict) -> int:
    return sum(len(re.sub(r"\[/?nl\]", "", t["text"]).split()) for t in data["turns"])


def call_text_model(client, prompt: str) -> str:
    from google.genai import types
    last = None
    for model in TEXT_MODELS:
        try:
            resp = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", temperature=0.9, max_output_tokens=30000))
            if resp.text:
                log(f"  text model: {model}")
                return resp.text
        except Exception as ex:
            last = ex
            log(f"  text model {model} failed: {str(ex)[:200]}")
    raise RuntimeError(f"all text models failed: {last}")


def generate_script(client, prompt: str) -> dict:
    best = None
    for attempt in range(3):
        try:
            data = extract_json(call_text_model(client, prompt))
            validate_script(data)
        except Exception as ex:
            log(f"  script attempt {attempt + 1} invalid: {ex}")
            continue
        words = word_count(data)
        log(f"  script attempt {attempt + 1}: {words} words")
        if best is None or words > word_count(best):
            best = data
        if words >= TARGET_WORDS * 0.75:
            return data
        prompt += (f"\n\nYour previous draft had only {words} words. Write at least {TARGET_WORDS} words: "
                   "more depth per story, more banter, fuller lessons. Same JSON format.")
    if best is None:
        raise RuntimeError("could not get a valid script from the model")
    return best


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
        if cur and size + n > limit:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def tts_gemini(client, turns: list[dict], workdir: Path) -> list[Path]:
    from google.genai import types
    voices = {"Anna": "Kore", "Ben": "Puck"}
    speech = types.SpeechConfig(multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
        speaker_voice_configs=[
            types.SpeakerVoiceConfig(speaker=name, voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=v)))
            for name, v in voices.items()]))
    chunks = chunk_turns(turns)
    paths: list[Path] = []
    model_idx = 0
    for n, chunk in enumerate(chunks, 1):
        text = ("TTS the following conversation between Anna and Ben. Relaxed, warm, witty podcast banter, "
                "natural pacing; Dutch words pronounced correctly in Dutch:\n" +
                "\n".join(f"{t['speaker']}: {strip_tags(t['text'])}" for t in chunk))
        pcm = None
        for attempt in range(5):
            model = TTS_MODELS[min(model_idx, len(TTS_MODELS) - 1)]
            try:
                resp = client.models.generate_content(
                    model=model, contents=text,
                    config=types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=speech))
                pcm = resp.candidates[0].content.parts[0].inline_data.data
                if pcm:
                    break
            except Exception as ex:
                msg = str(ex)
                log(f"  TTS chunk {n}/{len(chunks)} attempt {attempt + 1} on {model}: {msg[:160]}")
                if ("NOT_FOUND" in msg or "404" in msg) and model_idx < len(TTS_MODELS) - 1:
                    model_idx += 1          # model name retired: next one in the list
                    continue
                time.sleep(30 * (attempt + 1))
        if not pcm:
            raise RuntimeError(f"Gemini TTS failed on chunk {n}")
        wav = workdir / f"chunk_{n:03d}.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
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

    jobs: list[tuple[Path, str, str]] = []
    order: list[Path] = []
    for ti, t in enumerate(turns):
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
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return path.stat().st_size / (64000 / 8)


def make_audio(client, turns: list[dict], out: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        engine = "gemini"
        try:
            if client is None:
                raise RuntimeError("no Gemini client")
            parts = tts_gemini(client, turns, work)
        except Exception as ex:
            log(f"Gemini TTS unavailable ({str(ex)[:200]}); falling back to Edge TTS")
            engine = "edge"
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
            f"<description>{escape(e['notes'])}</description>"
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
    lines = ["News:"] + [f"- {s}" for s in script["covered_stories"]]
    if script["random_topic"]:
        lines.append(f"Random topic: {script['random_topic']}")
    lines += [f"Dutch: {dutch}", f"Dzogchen: {dzogchen}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="site")
    ap.add_argument("--dry-run", action="store_true", help="fetch feeds and print prompt only")
    ap.add_argument("--no-audio", action="store_true", help="write the script only; no state changes")
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

    if existing:                                  # forced re-run keeps the same lesson themes
        d_idx, z_idx = existing["dutch_idx"], existing["dzogchen_idx"]
    else:
        d_idx = state["dutch_next"] % len(curriculum["dutch"])
        z_idx = state["dzogchen_next"] % len(curriculum["dzogchen"])
    dutch_theme, dzogchen_theme = curriculum["dutch"][d_idx], curriculum["dzogchen"][z_idx]

    prompt = build_prompt(today, items, recent, dutch_theme, dzogchen_theme, curriculum["dutch_level"])
    if args.dry_run:
        print(prompt)
        return 0

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log("GEMINI_API_KEY is not set.")
        return 1
    from google import genai
    client = genai.Client(api_key=api_key)

    log("Writing script...")
    script = generate_script(client, prompt)

    if args.no_audio:
        text = "\n\n".join(f"{t['speaker']}: {t['text']}" for t in script["turns"])
        Path("script.txt").write_text(text, encoding="utf-8")
        print(text)
        log(f"\n{word_count(script)} words -> script.txt")
        return 0

    fname = f"{today.isoformat()}.mp3"
    out = root / "episodes" / fname
    log("Generating audio...")
    engine = make_audio(client, script["turns"], out)
    log(f"  audio engine: {engine}")

    by_id = {f"{code}{n}": i for code, lst in items.items() for n, i in enumerate(lst, 1)}
    used = [by_id[x] for x in script["used_ids"] if x in by_id]
    entry = {
        "date": today.isoformat(), "title": script["title"], "file": fname,
        "published": now.isoformat(), "size": out.stat().st_size, "duration": duration_seconds(out),
        "stories": script["covered_stories"], "random_topic": script["random_topic"],
        "links": [i["link"] for i in used], "item_titles": [i["title"] for i in used],
        "dutch_idx": d_idx, "dzogchen_idx": z_idx,
        "notes": make_notes(script, dutch_theme, dzogchen_theme), "engine": engine,
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

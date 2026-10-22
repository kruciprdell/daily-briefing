# Daily Briefing podcast: setup (about 20 minutes, one time)

Runs on GitHub Actions every morning at ~05:30 Amsterdam time, publishes an mp3 + RSS feed to GitHub Pages.
Your phone subscribes once and the episode is just there.

## 1. Gemini API key (free)
Create a key at https://aistudio.google.com/apikey

## 2. GitHub repo
1. Create a new **public** repo (free Pages needs public), e.g. `daily-briefing`.
2. Upload every file from this folder, keeping the structure (including `.github/workflows/daily.yml`).
3. Repo -> Settings -> Secrets and variables -> Actions:
   - **Secrets** tab -> New secret: `GEMINI_API_KEY` = your key
   - **Variables** tab -> New variable: `FEED_DIR` = a random string, e.g. `k7x2m9q4` (this becomes part of your feed URL)

## 3. First run
1. Actions tab -> `daily-podcast` -> Run workflow. Takes ~5-10 minutes (the free TTS tier is rate-limited, so it paces itself).
2. When it is green: Settings -> Pages -> Source: **Deploy from a branch** -> branch `gh-pages`, folder `/ (root)` -> Save.
3. Your feed: `https://<your-github-name>.github.io/<repo-name>/<FEED_DIR>/feed.xml`

## 4. Subscribe on Android
AntennaPod -> Add podcast -> "Add podcast by RSS address" -> paste the feed URL.
Turn on auto-download for the feed. Windows: open the same feed URL in Pocket Casts web, or play the mp3 from the browser.

## Test locally (optional, Windows/Mac/Linux)
```
pip install -r requirements.txt
set GEMINI_API_KEY=your_key        (PowerShell: $env:GEMINI_API_KEY="your_key")
python generate.py --dry-run       # fetches the feeds, prints the prompt, no API call
python generate.py --no-audio      # writes script.txt only; judge the writing before spending TTS quota
```
Full local audio runs need `ffmpeg` on the PATH.

## Tuning (edit in the repo, no code needed)
- `feeds.json`: add or remove RSS sources per region.
- `curriculum.json`: the Dutch and Dzogchen themes, one per day in order, then it loops. `dutch_level` sets the difficulty.
- Env vars in `daily.yml`: `STORIES_PER_REGION` (default 5), `TARGET_WORDS` (default 3000, about 20 min), `KEEP_DAYS` (default 14).

## How the pieces behave
- **No repeats:** each episode stores the links and headlines it used. For 7 days those are removed from the candidate list, and the script prompt also gets the list as "blocked" (a story returns only with a new development).
- **Repo size:** each run force-pushes a single-commit `gh-pages` branch with only the last 14 mp3s. Old audio never piles up in git history.
- **Voices:** Gemini multi-speaker TTS (Anna + Ben). If it fails or the free quota is gone, the run falls back to Edge TTS (flatter voices, but proper Dutch voices for the Dutch phrases).

## Things that can break
- **Model names change.** The script tries a list of Gemini models in order. If all are retired, set repo variables `GEMINI_TEXT_MODELS` / `GEMINI_TTS_MODELS` (comma-separated) and add them to the `env:` block in `daily.yml`.
- **A feed URL dies:** the run continues without it; the log says which. Replace it in `feeds.json`.
- **GitHub pauses scheduled workflows** in a repo after ~60 days without repo activity. If episodes stop, open Actions and re-enable the workflow.
- **The feed is public.** The random `FEED_DIR` only keeps it unlisted; it is visible to anyone who browses the repo. Fine for this content, not for anything private.

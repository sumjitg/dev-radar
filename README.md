# Dev Radar

A two-hourly sweep for what just shipped: RSS feeds plus the
[agent-reach](https://github.com/Panniantong/Agent-Reach) skill for searches and
full-page reads, ranked by recency and keywords, published as a page and pushed
to your phone.

## What one run does

1. **Collect** every feed in `config.json` (parallel, standard library only).
2. **Reach** - if `agent-reach` is installed, run each `agent_reach.searches`
   entry (`agent-reach web search "..." --limit 5`) and read the full page for
   the thinnest items (`agent-reach read <url>`), so cards carry real text and
   not just an RSS snippet.
3. **Prepare** - de-duplicate by normalised URL and by title, drop anything
   older than `lookback_hours`, score by recency + keyword boosts, mark what
   arrived since the last two-hour window as `new`.
4. **Publish** - `public/index.html` (searchable, tag filters, "new only"),
   `public/data.json` (machine readable) and `public/digest.md`.
5. **Notify** - optional ntfy push and/or Telegram message with the top items.

Every step degrades: a dead feed, a blocked page or a missing `agent-reach`
is logged and skipped, and the run still publishes what it has.

## The schedule

`.github/workflows/radar.yml` runs on `cron: "0 */2 * * *"` - every two hours on
the even UTC hour - on every push to `main`, and when started by hand from
**Actions → Dev Radar → Run workflow**. GitHub can start a scheduled run a few
minutes late, and skips it entirely if the repository has had no activity for 60
days, so the workflow commits a `.keepalive` file once a day to keep the
schedule enabled.

## One-time setup on GitHub

1. **Actions** tab → *Dev Radar* → **Run workflow** to fill the page once.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions** - the
   page then lives at `https://<your-user>.github.io/<repo>/`.
3. Optional secrets (**Settings → Secrets and variables → Actions**):
   - `NTFY_TOPIC` - push notifications. Install the [ntfy](https://ntfy.sh) app,
     subscribe to a private topic name of your choosing, and put that same name
     here. No account needed.
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` - Telegram delivery instead of,
     or as well as, ntfy.
   - `OPENAI_API_KEY` or `GROQ_API_KEY` - turns the summary block into a written
     brief instead of a plain list of the top headlines. Without a key the page
     still works; the brief just lists headlines.

Nothing is required for the page itself: with no secrets it still collects,
ranks and publishes, it just does not notify anyone.

## Change what it watches

Everything lives in `config.json`:

- `feeds` - `name`, `url`, and `categories` (which become the tag chips). Any
  RSS or Atom feed works.
- `agent_reach.searches` - `channel` + `action` + `query` + `limit`, plus
  `categories`. Defaults are two `web` searches; drop them, add more, or use
  another channel that needs no login (`github`, `youtube`, `v2ex`).
  `agent_reach.read_top_n` caps how many pages are opened per run,
  `timeout_seconds` caps each call, and `read_backend` (`public-web` by default)
  keeps page reads on the plain web path instead of a platform API, which
  rate-limits CI runners.
- `ranking.boost_keywords` / `demote_keywords` - what floats up, what sinks.
- `lookback_hours`, `max_items`, `schedule_hours` (keep it equal to the cron
  interval so "new" lines up with the previous run), `timezone` for the clock
  shown on the page.

## Run it locally

```bash
python3 radar.py          # writes public/
open public/index.html    # or: python3 -m http.server -d public 8000
```

`agent-reach` is optional locally too. To install it:

```bash
uv tool install https://github.com/Panniantong/Agent-Reach/releases/latest/download/agent-reach.zip
agent-reach setup
agent-reach doctor --text
```

Only channels that need no login work in CI (no browser profile lives there):
`web`, `github`, `youtube` (without speech-to-text), `v2ex`. Login-walled
channels - X, Reddit, LinkedIn, Xiaohongshu - give `NEED_LOGIN` on a runner and
are skipped with a log line.

## Files

- `radar.py` - the whole pipeline, standard library only.
- `config.json` - everything you can tune.
- `templates/index.html` - the page shell; `radar.py` fills in the cards.
- `.github/workflows/radar.yml` - the two-hourly run and the Pages deploy.

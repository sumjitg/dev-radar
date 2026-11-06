# Dev Radar

A two-hourly sweep of everything moving in AI - startups and money, papers and
research, frontier labs and AGI, India's AI build-out, and what people are
arguing about on Reddit and Hacker News. RSS feeds plus the
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
   older than `lookback_hours`, score by recency + keyword boosts, cap each
   topic so no single source floods the page, and mark the items published in
   the two hours before the current even hour as `new` - each slot is shown
   once, so nothing repeats from run to run.
4. **Publish** - `public/index.html` grouped by topic, newest first, showing
   **only new items** with everything from the rest of the day behind an
   "Earlier" toggle; plus `public/data.json` and `public/digest.md`.
5. **Notify** - optional ntfy push and/or Telegram message with the top items.

The page covers six topics in one sweep: **AI startups & money**, **AI
research** (arXiv, papers, labs), **Frontier labs & AGI**, **India AI**
(national news, policy, chips, data centres), **AI in the world**, and **what
people are saying** (Reddit, Hacker News, Agent Reach web searches).

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
   page then lives at `https://<your-user>.github.io/<repo>/`. GitHub only
   offers Pages for private repositories on a paid plan; on a free plan either
   make this repository public or skip Pages - the digest is still generated on
   every run and attached to the run as the `github-pages` artifact, and the
   notification path works either way.
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

- `topics` - ordered `{ "id": ..., "label": ... }` groups. A feed's first
  `categories` entry is the topic it files under, so `["india"]` lands in the
  India AI section and `["discussion"]` in what people are saying. Order here is
  the order of the sections on the page.
- `feeds` - `name`, `url`, `categories`, and optionally `user_agent` (Reddit
  wants a descriptive one) and `require_ai` (`true` drops posts that never
  mention AI - used on the general-news feeds so a Hacker News front page full
  of rocket launches does not fill the radar). Any RSS or Atom feed works,
  including Google News query feeds such as
  `https://news.google.com/rss/search?q=India%20AI%20policy&hl=en-IN&gl=IN&ceid=IN:en`
  - that is how the India and superintelligence sections stay broad.
- `agent_reach.searches` - `channel` + `action` + `query` + `limit` + `categories`,
  and `enabled: false` to park one. Defaults are four `web` searches (debates,
  startup funding, India, superintelligence) plus a disabled `twitter` search.
  `agent_reach.read_top_n` caps how many pages are opened per run and
  `timeout_seconds` caps each call. The workflow hands the run a
  `GITHUB_TOKEN`, so GitHub page reads do not hit the 60-per-hour anonymous
  limit.
- `ranking.boost_keywords` / `demote_keywords` - what floats up, what sinks.
- `only_new` (`true`) - the page shows only items since the previous sweep; set
  it to `false` to list the whole `lookback_hours` window.
- `max_per_topic` - the most cards one topic may contribute per run.
- `lookback_hours`, `max_items`, `schedule_hours` (keep it equal to the cron
  interval so "new" lines up with the previous run), `timezone` for the clocks
  shown on the page.

### X / Twitter

X needs a logged-in session, so a GitHub runner cannot search it: the `twitter`
search in `config.json` ships disabled. Run
`agent-reach login twitter` locally and set `"enabled": true` on that entry to
use it outside CI. Reddit is covered without a login through its public RSS
feeds, which is where most of the "what people are saying" cards come from.

## The Prospects tab

The page has two tabs: **Radar** (the two-hourly feed) and **Prospects** (a
hand-researched outreach list). The list lives in `prospects.json` - one object
per company with `company`, `tier`, `fit`, `city`, `phone`, `email`, `website`,
`rating`, `reviews`, `rating_source`, `decision_maker`, `role`, `trigger` (what
was actually seen on their site) and `gap` (what to pitch). `radar.py` renders
it on every run, so the tab is never overwritten: edit the JSON, push, and the
next run shows the change. A missing or broken file is logged and the tab
renders empty. The same list is published as `public/prospects.csv` behind the
tab's **Download CSV** link.

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
- `prospects.json` - the hand-researched prospect list behind the **Prospects** tab.
- `templates/index.html` - the page shell; `radar.py` fills in the cards.
- `.github/workflows/radar.yml` - the two-hourly run and the Pages deploy.

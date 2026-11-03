#!/usr/bin/env python3
"""Dev Radar - a two-hourly digest of what just shipped.

Pipeline:
  1. collect  RSS/Atom feeds listed in config.json (threaded, stdlib only)
  2. reach    optional: the `agent-reach` skill for searches and full page reads
  3. prepare  de-duplicate, drop stale items, rank by recency and keywords
  4. publish  render public/index.html, public/data.json and public/digest.md
  5. notify   optional: ntfy push and/or a Telegram message

Nothing here fails the run because one source is down - every step degrades to
whatever was collected. Standard library only; the one external program it may
call is `agent-reach`, and it is skipped cleanly when it is not installed.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
TEMPLATE_PATH = ROOT / "templates" / "index.html"
PUBLIC_DIR = ROOT / "public"

USER_AGENT = "DevRadar/1.0 (two-hourly digest bot; +https://github.com)"
HTTP_TIMEOUT = 25
TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref_src",
    "cmpid",
    "smid",
    "guccounter",
    "at_medium",
    "at_campaign",
    "igshid",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def log(message: str) -> None:
    print(f"[radar] {message}", flush=True)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    return (
        value.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_date(raw: str | None) -> dt.datetime | None:
    """Parse an RSS/Atom date string into an aware UTC datetime."""
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None

    parsed: dt.datetime | None = None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        parsed = None

    if parsed is None:
        cleaned = text.replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(cleaned)
        except ValueError:
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed = dt.datetime.strptime(cleaned, fmt)
                    break
                except ValueError:
                    continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def flat(raw: str | None, limit: int = 0) -> str:
    """Strip markup, collapse whitespace and optionally trim on a word boundary."""
    if not raw:
        return ""
    text = html_lib.unescape(_TAG_RE.sub(" ", raw))
    text = _WS_RE.sub(" ", text).strip()
    if limit and len(text) > limit:
        cut = text[:limit]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut.rstrip(" ,;:-–—") + "…"
    return text


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    if not parts.scheme or not parts.netloc:
        return url.strip()
    kept = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            urllib.parse.urlencode(kept),
            "",
        )
    )


def title_key(title: str) -> str:
    return _NON_ALNUM_RE.sub(" ", (title or "").lower()).strip()


def unique(values) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def http_get(url: str, timeout: int = HTTP_TIMEOUT) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def slot_start(moment: dt.datetime, hours: int) -> dt.datetime:
    """The start of the current scheduled window, e.g. 14:00 for a run at 14:07."""
    step = max(1, int(hours)) * 3600
    return dt.datetime.fromtimestamp((moment.timestamp() // step) * step, tz=dt.timezone.utc)


def human_age(published: dt.datetime | None, moment: dt.datetime) -> str:
    if published is None:
        return "time unknown"
    seconds = max(0.0, (moment - published).total_seconds())
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(minutes, 1)}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def public_url() -> str:
    explicit = os.environ.get("RADAR_PUBLIC_URL", "").strip()
    if explicit:
        return explicit.rstrip("/") + "/"
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    owner = owner.lower()
    if name.lower() == f"{owner}.github.io":
        return f"https://{owner}.github.io/"
    return f"https://{owner}.github.io/{name}/"


def local_clock(moment: dt.datetime, tz_name: str) -> str:
    try:
        zone = ZoneInfo(tz_name)
    except Exception:  # unknown zone name in config
        return ""
    local = moment.astimezone(zone)
    return f"{local.strftime('%d %b %H:%M')} {local.tzname() or ''}".strip()


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"config.json is missing at {CONFIG_PATH}")
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------- #
# feed collection
# --------------------------------------------------------------------------- #
def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1].lower()


def _child(elem, names):
    wanted = {name.lower() for name in names}
    for child in list(elem):
        if isinstance(child.tag, str) and _local(child.tag) in wanted:
            return child
    return None


def _child_text(elem, *names) -> str:
    node = _child(elem, names)
    return "".join(node.itertext()) if node is not None else ""


def _item_url(elem) -> str:
    for child in list(elem):
        if not isinstance(child.tag, str) or _local(child.tag) != "link":
            continue
        rel = (child.get("rel") or "alternate").lower()
        href = (child.get("href") or "").strip() or (child.text or "").strip()
        if href and rel in ("alternate", ""):
            return href
    fallback = _child_text(elem, "guid", "id").strip()
    return fallback if fallback.startswith("http") else ""


def parse_feed(raw: bytes, source: str, categories: list[str]) -> list[dict]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        log(f"feed parse error ({source}): {exc}")
        return []

    items: list[dict] = []
    for elem in root.iter():
        if not isinstance(elem.tag, str) or _local(elem.tag) not in ("item", "entry"):
            continue
        title = flat(_child_text(elem, "title"), 300)
        if not title:
            continue
        url = _item_url(elem).strip()
        if not url:
            continue
        items.append(
            {
                "title": title,
                "url": url,
                "source": source,
                "published": iso(
                    parse_date(
                        _child_text(
                            elem,
                            "pubdate",
                            "published",
                            "updated",
                            "date",
                            "created",
                            "issued",
                            "modified",
                        )
                    )
                ),
                "summary": flat(
                    _child_text(elem, "description", "summary", "content", "encoded"),
                    400,
                ),
                "tags": unique([tag.lower() for tag in categories]),
                "via": "rss",
            }
        )
    return items


def collect_feeds(cfg: dict) -> list[dict]:
    feeds = [feed for feed in cfg.get("feeds") or [] if feed.get("url")]
    if not feeds:
        return []
    timeout = int(cfg.get("http_timeout_seconds") or HTTP_TIMEOUT)

    def fetch(feed: dict) -> list[dict]:
        name = feed.get("name") or urllib.parse.urlsplit(feed["url"]).netloc
        try:
            raw = http_get(feed["url"], timeout=timeout)
            items = parse_feed(raw, name, feed.get("categories") or [])
        except Exception as exc:  # one broken feed must never sink the run
            log(f"feed failed ({name}): {exc}")
            return []
        log(f"feed ok ({name}): {len(items)} items")
        return items

    collected: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(feeds))) as pool:
        for chunk in pool.map(fetch, feeds):
            collected.extend(chunk)
    return collected


# --------------------------------------------------------------------------- #
# agent-reach
# --------------------------------------------------------------------------- #
def agent_reach_binary() -> str | None:
    override = os.environ.get("AGENT_REACH_BIN", "").strip()
    if override and Path(override).exists():
        return override
    return shutil.which("agent-reach")


def _json_from_output(text: str) -> dict | None:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def run_agent_reach(args: list[str], timeout: int) -> dict | None:
    binary = agent_reach_binary()
    if not binary:
        return None
    try:
        completed = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log(f"agent-reach timed out after {timeout}s: {' '.join(args)}")
        return None
    except OSError as exc:
        log(f"agent-reach could not start: {exc}")
        return None

    payload = _json_from_output(completed.stdout) or _json_from_output(completed.stderr)
    if payload is None:
        log(f"agent-reach gave no JSON for '{' '.join(args)}' (exit {completed.returncode})")
        return None
    if payload.get("ok") is False:
        error = payload.get("error") or {}
        log(
            "agent-reach error "
            f"{error.get('code') or '?'} for '{' '.join(args)}': "
            f"{error.get('message') or error.get('hint') or 'no detail'}"
        )
        return None
    return payload


def _find_items(node, depth: int = 0) -> list | None:
    if depth > 6:
        return None
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "items" and isinstance(value, list):
                return value
            found = _find_items(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_items(value, depth + 1)
            if found is not None:
                return found
    return None


def _longest_text(node, depth: int = 0) -> str:
    if depth > 8:
        return ""
    if isinstance(node, str):
        return node
    best = ""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("raw", "meta"):
                continue
            candidate = _longest_text(value, depth + 1)
            if len(candidate) > len(best):
                best = candidate
    elif isinstance(node, list):
        for value in node:
            candidate = _longest_text(value, depth + 1)
            if len(candidate) > len(best):
                best = candidate
    return best


def reach_items_from(payload: dict, channel: str, query: str, categories: list[str]) -> list[dict]:
    found = _find_items(payload) or []
    label = f"reach:{channel}" + (f" · {query}" if query else "")
    out: list[dict] = []
    for raw in found:
        if not isinstance(raw, dict):
            continue
        title = flat(str(raw.get("title") or raw.get("name") or ""), 300)
        url = str(raw.get("url") or raw.get("link") or "").strip()
        if not title or not url:
            continue
        out.append(
            {
                "title": title,
                "url": url,
                "source": label,
                "published": iso(
                    parse_date(
                        str(
                            raw.get("published_at")
                            or raw.get("published")
                            or raw.get("created_at")
                            or ""
                        )
                    )
                ),
                "summary": flat(
                    str(raw.get("text") or raw.get("summary") or raw.get("excerpt") or ""),
                    400,
                ),
                "tags": unique([tag.lower() for tag in categories]),
                "via": "agent-reach",
            }
        )
    return out


def collect_reach(cfg: dict) -> list[dict]:
    reach = cfg.get("agent_reach") or {}
    searches = reach.get("searches") or []
    if not reach.get("enabled") or not searches:
        return []
    if not agent_reach_binary():
        log("agent-reach not installed - continuing with RSS feeds only")
        return []

    timeout = int(reach.get("timeout_seconds") or 80)
    collected: list[dict] = []
    for search in searches:
        channel = str(search.get("channel") or "web")
        action = str(search.get("action") or "search")
        query = str(search.get("query") or "").strip()
        if not query:
            continue
        args = [channel, action, query]
        if search.get("limit"):
            args.extend(["--limit", str(int(search["limit"]))])
        args.extend([str(extra) for extra in search.get("extra_args") or []])
        payload = run_agent_reach(args, timeout)
        if not payload:
            continue
        items = reach_items_from(payload, channel, query, search.get("categories") or [])
        log(f"agent-reach search ({channel}: {query}): {len(items)} items")
        collected.extend(items)
    return collected


def enrich(items: list[dict], cfg: dict) -> None:
    """Read the full page for the thinnest items, so cards carry real text."""
    reach = cfg.get("agent_reach") or {}
    limit = int(reach.get("read_top_n") or 0)
    if not reach.get("enabled") or limit <= 0 or not items:
        return
    if not agent_reach_binary():
        return

    timeout = int(reach.get("timeout_seconds") or 80)
    targets = [item for item in items if len(item.get("summary") or "") < 160][:limit]
    if not targets:
        return
    log(f"agent-reach read: enriching {len(targets)} item(s)")
    for item in targets:
        payload = run_agent_reach(["read", item["url"]], timeout)
        if not payload:
            continue
        body = flat(_longest_text(payload), 2400)
        if len(body) > len(item.get("summary") or ""):
            item["detail"] = body
            item["summary"] = flat(body, 400)


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #
def score_item(item: dict, cfg: dict, moment: dt.datetime) -> float:
    ranking = cfg.get("ranking") or {}
    boosts = {
        str(key).lower(): float(value)
        for key, value in (ranking.get("boost_keywords") or {}).items()
    }
    demotes = [str(key).lower() for key in ranking.get("demote_keywords") or []]

    published = parse_date(item.get("published"))
    age_hours = (moment - published).total_seconds() / 3600.0 if published else 12.0
    recency = max(0.0, 1.0 - min(age_hours, 36.0) / 36.0) * 3.0

    haystack = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    boost = sum(weight for word, weight in boosts.items() if word in haystack)
    penalty = sum(2.0 for word in demotes if word in haystack)
    bonus = 1.0 if item.get("via") == "agent-reach" else 0.0

    return round(recency + min(boost, 4.0) + bonus - penalty, 3)


def prepare(raw_items: list[dict], cfg: dict, moment: dt.datetime) -> list[dict]:
    window_hours = int(cfg.get("lookback_hours") or 24)
    max_items = int(cfg.get("max_items") or 60)
    cutoff = moment - dt.timedelta(hours=window_hours)
    fresh_from = slot_start(moment, int(cfg.get("schedule_hours") or 2))

    kept: list[dict] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    stale = 0
    duplicate = 0

    for item in raw_items:
        url_key = normalize_url(item.get("url") or "")
        key = title_key(item.get("title") or "")
        if not url_key or not key:
            continue
        if url_key in seen_urls or key in seen_titles:
            duplicate += 1
            continue

        published = parse_date(item.get("published"))
        if published is not None and published < cutoff:
            stale += 1
            continue
        if published is not None and published > moment + dt.timedelta(hours=1):
            published = moment  # feed clock skew

        item["published"] = iso(published)
        item["detail"] = item.get("detail") or ""
        item["tags"] = unique([str(tag).lower() for tag in item.get("tags") or []])
        item["score"] = score_item(item, cfg, moment)
        item["is_new"] = bool(published and published >= fresh_from)
        item["age"] = human_age(published, moment)

        seen_urls.add(url_key)
        seen_titles.add(key)
        kept.append(item)

    kept.sort(key=lambda entry: (entry.get("score", 0.0), entry.get("published") or ""), reverse=True)
    if stale or duplicate:
        log(f"prepare: dropped {stale} stale and {duplicate} duplicate item(s)")
    return kept[:max_items]


# --------------------------------------------------------------------------- #
# optional LLM brief
# --------------------------------------------------------------------------- #
def llm_brief(items: list[dict], cfg: dict) -> str:
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not items or not (openai_key or groq_key):
        return ""

    if openai_key:
        endpoint = "https://api.openai.com/v1/chat/completions"
        api_key = openai_key
        model = os.environ.get("OPENAI_MODEL", "").strip() or "gpt-4o-mini"
    else:
        endpoint = "https://api.groq.com/openai/v1/chat/completions"
        api_key = groq_key
        model = os.environ.get("GROQ_MODEL", "").strip() or "llama-3.3-70b-versatile"

    lines = [
        f"- {item.get('title', '')} [{item.get('source', '')}]: {flat(item.get('summary') or '', 220)}"
        for item in items[:18]
    ]
    body = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 400,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a terse news editor for a developer. You get headlines "
                    "collected in the last few hours. Reply with 3 to 5 short bullet "
                    "lines (start each with '- ') naming the developments that actually "
                    "matter, with the concrete detail (version, model, company) when the "
                    "input has it. No preamble, no headings, no closing line."
                ),
            },
            {"role": "user", "content": "\n".join(lines)},
        ],
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        text = payload["choices"][0]["message"]["content"]
    except (urllib.error.URLError, KeyError, IndexError, TypeError, ValueError) as exc:
        log(f"llm brief skipped: {exc}")
        return ""
    log(f"llm brief: {len(text)} chars from {model}")
    return str(text).strip()


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def brief_markup(brief: str, items: list[dict]) -> str:
    if brief:
        bullets = [
            line.strip().lstrip("-*•").strip()
            for line in brief.splitlines()
            if line.strip()
        ]
        bullets = [bullet for bullet in bullets if bullet]
    else:
        bullets = [
            f"{item.get('title', '')} — {item.get('source', '')}" for item in items[:4]
        ]
    if not bullets:
        return ""
    entries = "".join(f"<li>{html_lib.escape(bullet)}</li>" for bullet in bullets)
    label = "Today, in short" if brief else "Latest headlines"
    return f'<h2 class="brief-title">{label}</h2><ul class="brief-list">{entries}</ul>'


def chips_markup(items: list[dict]) -> str:
    counts: dict[str, int] = {}
    for item in items:
        for tag in item.get("tags") or []:
            counts[tag] = counts.get(tag, 0) + 1
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:10]
    parts = ['<button class="chip is-active" data-tag="">All</button>']
    parts.append('<button class="chip" id="new-only" type="button">New only</button>')
    for tag, _count in ordered:
        parts.append(
            f'<button class="chip" data-tag="{html_lib.escape(tag)}">#{html_lib.escape(tag)}</button>'
        )
    return "".join(parts)


def cards_markup(items: list[dict]) -> str:
    if not items:
        return (
            '<p class="empty">Nothing collected yet. The next run fills this in - '
            'or run the workflow by hand from the Actions tab.</p>'
        )

    cards: list[str] = []
    for item in items:
        title = html_lib.escape(item.get("title", ""))
        url = html_lib.escape(item.get("url", ""))
        source = html_lib.escape(item.get("source", ""))
        summary = html_lib.escape(item.get("summary") or "")
        age = html_lib.escape(item.get("age") or "")
        published = html_lib.escape(item.get("published") or "")
        tags = item.get("tags") or []
        tag_list = ",".join(html_lib.escape(tag) for tag in tags)
        tag_html = "".join(
            f'<span class="tag">#{html_lib.escape(tag)}</span>' for tag in tags[:4]
        )
        searchable = html_lib.escape(
            " ".join(
                [item.get("title", ""), source, item.get("summary") or "", " ".join(tags)]
            ).lower()
        )
        is_new = "1" if item.get("is_new") else "0"
        badge = '<span class="badge">new</span>' if item.get("is_new") else ""

        detail = item.get("detail") or ""
        extra = ""
        if detail and len(detail) > len(item.get("summary") or "") + 80:
            extra = (
                '<details class="more"><summary>Full text</summary>'
                f"<p>{html_lib.escape(detail)}</p></details>"
            )
        summary_html = f'<p class="sum">{summary}</p>' if summary else ""
        new_class = " is-new" if item.get("is_new") else ""

        cards.append(
            f'<article class="card{new_class}" '
            f'data-tags="{tag_list}" data-new="{is_new}" data-q="{searchable}">'
            '<div class="row">'
            f'<span class="src">{source}</span>'
            f'<span class="age"><time datetime="{published}">{age}</time></span>'
            f"{badge}"
            "</div>"
            f'<h3><a href="{url}" target="_blank" rel="noopener">{title}</a></h3>'
            f"{summary_html}"
            f"{extra}"
            f'<div class="tags">{tag_html}</div>'
            "</article>"
        )
    return "\n".join(cards)


def render_page(
    items: list[dict],
    cfg: dict,
    moment: dt.datetime,
    window_hours: int,
    brief: str,
) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    new_count = sum(1 for item in items if item.get("is_new"))
    title = cfg.get("title") or "Dev Radar"
    tagline = cfg.get("tagline") or ""
    clock = local_clock(moment, cfg.get("timezone") or "UTC")

    replacements = {
        "{{TITLE}}": html_lib.escape(title),
        "{{TAGLINE}}": html_lib.escape(tagline),
        "{{UPDATED}}": html_lib.escape(moment.strftime("%d %b %Y, %H:%M UTC")),
        "{{UPDATED_LOCAL}}": html_lib.escape(clock),
        "{{UPDATED_ISO}}": html_lib.escape(iso(moment)),
        "{{COUNT}}": str(len(items)),
        "{{NEW_COUNT}}": str(new_count),
        "{{WINDOW}}": f"{window_hours}h",
        "{{BRIEF}}": brief_markup(brief, items),
        "{{CHIPS}}": chips_markup(items),
        "%%CARDS%%": cards_markup(items),
    }

    page = template
    for token, value in replacements.items():
        page = page.replace(token, value)
    return page


def markdown_digest(
    items: list[dict], cfg: dict, moment: dt.datetime, window_hours: int
) -> str:
    title = cfg.get("title") or "Dev Radar"
    new_count = sum(1 for item in items if item.get("is_new"))
    lines = [
        f"# {title}",
        "",
        f"_{moment.strftime('%d %b %Y, %H:%M UTC')} · last {window_hours}h · "
        f"{len(items)} items · {new_count} new_",
        "",
    ]
    if not items:
        lines.append("_No items collected in this window._")
    for item in items:
        marker = " **NEW**" if item.get("is_new") else ""
        lines.append(f"- [{item.get('title', '')}]({item.get('url', '')}) "
                     f"— {item.get('source', '')} · {item.get('age', '')}{marker}")
        summary = item.get("summary") or ""
        if summary:
            lines.append(f"  {summary}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #
def ascii_header(text: str, limit: int = 110) -> str:
    return text.encode("ascii", "replace").decode("ascii")[:limit]


def ntfy_push(topic: str, title: str, body: str, click: str) -> None:
    headers = {
        "Title": ascii_header(title),
        "Tags": "newspaper",
        "Priority": "default",
        "User-Agent": USER_AGENT,
    }
    if click:
        headers["Click"] = click
    request = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(topic)}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        response.read()


def telegram_push(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text[:3900],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        response.read()


def notify(
    items: list[dict], cfg: dict, moment: dt.datetime, brief: str, page: str
) -> None:
    delivery = cfg.get("delivery") or {}
    new_items = [item for item in items if item.get("is_new")]
    top = (new_items or items)[:6]
    if not top:
        return

    lines: list[str] = []
    if brief:
        lines.extend(
            line.strip().lstrip("-*•").strip()
            for line in brief.splitlines()
            if line.strip()
        )
        lines.append("")
    for item in top:
        lines.append(f"- {item.get('title', '')} ({item.get('source', '')}, {item.get('age', '')})")
    if page:
        lines.extend(["", page])
    body = "\n".join(lines)

    headline = (
        f"{len(new_items)} new development{'s' if len(new_items) != 1 else ''}"
        if new_items
        else f"{len(items)} developments in the last {int(cfg.get('lookback_hours') or 24)}h"
    )

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic and delivery.get("ntfy", True):
        try:
            ntfy_push(topic, headline, body, page)
            log(f"notify: ntfy sent ({len(body)} chars)")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"notify: ntfy failed: {exc}")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id and delivery.get("telegram", True):
        html_lines = [f"<b>{html_lib.escape(headline)}</b>", ""]
        for item in top:
            html_lines.append(
                f'• <a href="{html_lib.escape(item.get("url", ""))}">'
                f'{html_lib.escape(item.get("title", ""))}</a> '
                f'<i>{html_lib.escape(item.get("source", ""))}</i>'
            )
        if page:
            html_lines.extend(["", f'<a href="{html_lib.escape(page)}">Open the radar</a>'])
        try:
            telegram_push(token, chat_id, "\n".join(html_lines))
            log("notify: telegram sent")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"notify: telegram failed: {exc}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    cfg = load_config()
    moment = now_utc()
    window_hours = int(cfg.get("lookback_hours") or 24)

    raw = collect_feeds(cfg)
    raw.extend(collect_reach(cfg))
    log(f"collected {len(raw)} raw item(s)")

    items = prepare(raw, cfg, moment)
    log(f"prepared {len(items)} item(s) ({sum(1 for i in items if i.get('is_new'))} new)")

    enrich(items, cfg)
    brief = llm_brief(items, cfg)
    page = public_url()

    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    (PUBLIC_DIR / "index.html").write_text(
        render_page(items, cfg, moment, window_hours, brief), encoding="utf-8"
    )
    (PUBLIC_DIR / "data.json").write_text(
        json.dumps(
            {
                "title": cfg.get("title") or "Dev Radar",
                "generated": iso(moment),
                "window_hours": window_hours,
                "count": len(items),
                "new_count": sum(1 for item in items if item.get("is_new")),
                "brief": brief,
                "items": items,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (PUBLIC_DIR / "digest.md").write_text(
        markdown_digest(items, cfg, moment, window_hours), encoding="utf-8"
    )

    notify(items, cfg, moment, brief, page)
    log(f"published to {PUBLIC_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

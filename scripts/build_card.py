#!/usr/bin/env python3
"""Build the neofetch-style profile cards: dark_mode.svg and light_mode.svg.

What the card says lives in profile.json. The ASCII portraits live in
art/dark.txt and art/light.txt. GitHub numbers (repos, followers, lines of
code) are fetched live. Standard library only, so the GitHub Action needs
nothing installed.

Private repos: this repo and its Action logs are public, so private repo
names are never written to cache/stats.json or printed. Only totals show.

    GH_TOKEN=... python scripts/build_card.py   # fetch stats, write both SVGs
    python scripts/build_card.py --offline      # no network: reuse cache/stats.json
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache" / "stats.json"
API = "https://api.github.com"

# GitHub's own code colours, so the card matches the site in each theme.
THEMES = {
    "dark": dict(bg="#161b22", text="#c9d1d9", key="#ffa657", value="#a5d6ff",
                 cc="#616e7f", add="#3fb950", rem="#f85149"),
    "light": dict(bg="#f6f8fa", text="#24292f", key="#953800", value="#0a3069",
                  cc="#c2cfde", add="#1a7f37", rem="#cf222e"),
}

FONT = 16         # info text size (px)
LINE = 20         # info line spacing (px)
EM = 0.6          # monospace advance in em; Consolas is size-adjusted to match
PAD = 20          # card padding (px)
GAP = 28          # space between the portrait and the text (px)
STATS_LEFT = 35   # width of the left half of the two-column stats rows
FONT_STACK = ("ConsolasFallback, Menlo, 'DejaVu Sans Mono', 'Liberation Mono', "
              "'Courier New', monospace")


# ---------------------------------------------------------------- uptime

def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def uptime(since: date, today: date) -> str:
    """'2 years, 10 months, 21 days' between two dates."""
    months = (today.year - since.year) * 12 + today.month - since.month
    if add_months(since, months) > today:
        months -= 1
    days = (today - add_months(since, months)).days
    years, months = divmod(months, 12)

    def unit(n: int, word: str) -> str:
        return f"{n} {word}" + ("" if n == 1 else "s")

    return f"{unit(years, 'year')}, {unit(months, 'month')}, {unit(days, 'day')}"


# ---------------------------------------------------------------- GitHub stats

QUERY = """
query($login: String!, $cursor: String, $privacy: RepositoryPrivacy) {
  user(login: $login) {
    followers { totalCount }
    contributed: repositories(ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER],
                              privacy: $privacy) { totalCount }
    repositories(first: 100, after: $cursor, ownerAffiliations: [OWNER], privacy: $privacy) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { nameWithOwner }
    }
  }
}
"""


def call(url: str, token: str | None, body: dict | None = None):
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "profile-card-builder",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None)


def fetch_account(login: str, token: str | None, include_private: bool):
    variables = {"login": login, "cursor": None,
                 "privacy": None if include_private else "PUBLIC"}
    repos: list[dict] = []
    while True:
        _, data = call(f"{API}/graphql", token, {"query": QUERY, "variables": variables})
        if data.get("errors"):
            raise RuntimeError(data["errors"])
        user = data["data"]["user"]
        page = user["repositories"]
        repos += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        variables["cursor"] = page["pageInfo"]["endCursor"]
    totals = {"repos": page["totalCount"],
              "contributed": user["contributed"]["totalCount"],
              "followers": user["followers"]["totalCount"]}
    return totals, [r["nameWithOwner"] for r in repos]


def repo_key(name: str) -> str:
    """Cache key for a repo: a hash, so private repo names never land in this public repo."""
    return hashlib.sha256(name.lower().encode()).hexdigest()[:16]


def fetch_code(names: list[str], login: str, token: str | None, cached: dict) -> dict:
    """Lines added and deleted by `login` on each repo's default branch, by repo_key.

    GitHub computes these lazily and answers HTTP 202 until they are ready,
    so poll a few rounds. A repo that is still not ready keeps last run's numbers.
    Logs give counts only, never repo names.
    """
    done: dict[str, dict] = {}
    pending = list(names)
    failed = 0
    for attempt in range(6):
        waiting = []
        for name in pending:
            try:
                status, data = call(f"{API}/repos/{name}/stats/contributors", token)
            except urllib.error.HTTPError:
                failed += 1
                continue
            if status == 202:
                waiting.append(name)
                continue
            mine = next((p for p in data or []
                         if (p.get("author") or {}).get("login", "").lower() == login.lower()),
                        None)
            weeks = mine["weeks"] if mine else []
            done[repo_key(name)] = {"added": sum(w["a"] for w in weeks),
                                    "deleted": sum(w["d"] for w in weeks)}
        pending = waiting
        if not pending:
            break
        time.sleep(3 + 3 * attempt)
    if pending or failed:
        print(f"  {len(pending)} repo(s) still computing, {failed} failed: used saved numbers.",
              file=sys.stderr)
    for name in names:
        key = repo_key(name)
        if key not in done and key in cached:
            done[key] = cached[key]
    return done


def stats(cfg: dict, offline: bool) -> dict:
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    account, per_repo = cache.get("account"), cache.get("repos", {})
    if not offline:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        try:
            account, names = fetch_account(cfg["login"], token, cfg.get("include_private", False))
            per_repo = fetch_code(names, cfg["login"], token, per_repo)
            CACHE.parent.mkdir(exist_ok=True)
            with open(CACHE, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"account": account, "repos": per_repo}, f, indent=1, sort_keys=True)
                f.write("\n")
        except (urllib.error.URLError, RuntimeError, KeyError, TypeError) as err:
            if not account:
                raise
            print(f"GitHub stats failed ({err}); using the last saved numbers.", file=sys.stderr)
    if not account:
        sys.exit("No saved stats yet: run once with GH_TOKEN set.")
    # Repos that hold data dumps (CSV, notebook output) would swamp the line count.
    skip = {repo_key(n) for n in cfg.get("loc_exclude", [])}
    return {**account,
            "added": sum(r["added"] for k, r in per_repo.items() if k not in skip),
            "deleted": sum(r["deleted"] for k, r in per_repo.items() if k not in skip)}


# ---------------------------------------------------------------- card text

Segs = list  # list of (text, css class or None)


def field(key: str, value: Segs, width: int, lead: str = ". ") -> Segs:
    """'. Key: ....... value', dot-padded to exactly `width` characters."""
    vlen = sum(len(t) for t, _ in value)
    dots = max(1, width - len(lead) - len(key) - 3 - vlen)
    return [(lead, "cc"), (key, "key"), (":", None), (" " + "." * dots + " ", "cc")] + value


def info_rows(cfg: dict, s: dict, today: date) -> list[Segs]:
    width = cfg["width"]
    since = date.fromisoformat(cfg["uptime_since"])
    head = cfg["header"]
    rows = [[(head, "key"), (" " + "─" * (width - len(head) - 1), "cc")]]
    for i, block in enumerate(cfg["blocks"]):
        if i:
            rows.append([(".", "cc")])
        if block.get("title"):
            title = block["title"]
            rows.append([("- ", "cc"), (title, None), (" " + "─" * (width - len(title) - 3), "cc")])
        for key, value in block.get("rows", []):
            value = value.replace("{uptime}", uptime(since, today))
            rows.append(field(key, [(value, "value")], width))
        if block.get("stats"):
            rows += stats_rows(s, width)
    return rows


def stats_rows(s: dict, width: int) -> list[Segs]:
    def num(n: int) -> str:
        return f"{n:,}"

    right = width - STATS_LEFT - 3
    repos = [(num(s["repos"]), "value"), (" {", None), ("Contributed", "key"), (": ", None),
             (num(s["contributed"]), "value"), ("}", None)]
    loc = [(num(s["added"] - s["deleted"]), "value"), (" ( ", None),
           (num(s["added"]) + "++", "add"), (", ", None), (num(s["deleted"]) + "--", "rem"),
           (" )", None)]
    return [
        field("Repos", repos, STATS_LEFT) + [(" | ", None)]
        + field("Followers", [(num(s["followers"]), "value")], right, lead=""),
        field("Lines of Code", loc, width),
    ]


# ---------------------------------------------------------------- SVG

def spans(segs: Segs) -> str:
    return "".join(f'<tspan class="{cls}">{escape(t)}</tspan>' if cls else escape(t)
                   for t, cls in segs)


def render(theme: str, art: list[str], rows: list[Segs], width_chars: int) -> str:
    c = THEMES[theme]
    first = PAD + FONT                       # baseline of the first text row
    height = round(first + (len(rows) - 1) * LINE + PAD + 4)
    art_line = (height - 2 * PAD) / len(art)  # portrait fills the card height
    art_font = art_line / 1.2
    info_x = PAD + max(map(len, art)) * art_font * EM + GAP
    width = round(info_x + width_chars * FONT * EM + PAD)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{FONT_STACK}" font-size="{FONT}px" '
        'xml:space="preserve">',
        "<style>",
        "@font-face { font-family: 'ConsolasFallback'; src: local('Consolas'); size-adjust: 109%; }",
        "text, tspan { white-space: pre; }",
        f".key {{ fill: {c['key']}; }} .value {{ fill: {c['value']}; }} .cc {{ fill: {c['cc']}; }} "
        f".add {{ fill: {c['add']}; }} .rem {{ fill: {c['rem']}; }}",
        "</style>",
        f'<rect width="{width}" height="{height}" rx="15" fill="{c["bg"]}"/>',
        f'<text fill="{c["text"]}" font-size="{art_font:.2f}px">'
        + "".join(f'<tspan x="{PAD}" y="{PAD + art_line * (i + 0.8):.1f}">{escape(line)}</tspan>'
                  for i, line in enumerate(art))
        + "</text>",
    ]
    for i, segs in enumerate(rows):
        out.append(f'<text x="{info_x:.1f}" y="{first + i * LINE}" fill="{c["text"]}">'
                   f"{spans(segs)}</text>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Build dark_mode.svg and light_mode.svg.")
    ap.add_argument("--offline", action="store_true", help="reuse cache/stats.json, no network")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
    tz = timezone(timedelta(minutes=cfg.get("utc_offset_minutes", 0)))
    today = datetime.now(tz).date()
    s = stats(cfg, args.offline)
    rows = info_rows(cfg, s, today)
    for theme in ("dark", "light"):
        art = (ROOT / "art" / f"{theme}.txt").read_text(encoding="utf-8").splitlines()
        with open(ROOT / f"{theme}_mode.svg", "w", encoding="utf-8", newline="\n") as f:
            f.write(render(theme, art, rows, cfg["width"]))
    print(f"Built cards for {today}: " + ", ".join(f"{k} {v:,}" for k, v in s.items()))


if __name__ == "__main__":
    main()

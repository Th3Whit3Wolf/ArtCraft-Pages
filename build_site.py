#!/usr/bin/env python3
"""
Build a static GitHub Pages site hosting the web builds of the ArtCraft apps.

For every repo in the ArtCraft org (default: storytold), plus any community
apps listed in the ArtCraft Launcher catalogue, it:
  1. finds the newest release with a "<id>-web-<version>.zip" asset,
  2. downloads it (checking GitHub's SHA-256 digest), unpacks it into <out>/<id>/
     and prunes files that shouldn't be published (Cargo build leftovers,
     precompressed .gz/.br copies, _headers/.htaccess samples),
  3. sets that app's <title> to "<Project> <version>",
then writes <out>/index.html, a launcher page styled after ArtCraft Web
Launcher, plus <out>/apps.json.

Standard library only. Usage:
    GITHUB_TOKEN=... python scripts/build_site.py --out site
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent.parent

# Names, taglines, brand colours and repos of every Craft app, as ArtCraft
# Launcher shows them (vendored from storytold/craft-launcher by the
# artcraft-web-launcher project). Optional: without it, names come from repos.
DEFAULT_CATALOG_URL = (
    "https://raw.githubusercontent.com/AurelioB/artcraft-web-launcher/main/"
    "host/assets/vendor/craft-launcher/catalog.json"
)
ICON_URL = "https://getartcraft.com/images/apps/{id}/icon.webp"

# "<id>-web-<version>.zip", e.g. vectorcraft-web-0.24.1.zip
WEB_ASSET_RE = re.compile(r"^(?P<id>[A-Za-z0-9._-]+?)-web-v?(?P<version>\d[\w.+-]*)\.zip$", re.I)

# Things in upstream web zips that should not be published.
CARGO_MARKERS = (".fingerprint", ".cargo-lock")
CARGO_LEFTOVERS = (".fingerprint", ".cargo-lock", ".cargo-build-lock", ".cargo-artifact-lock",
                   "build", "deps", "examples", "incremental")
HOST_SAMPLES = ("_headers", ".htaccess")
PRECOMPRESSED = (".gz", ".br")

TEXT_EXTS = {".html", ".htm", ".js", ".mjs", ".css", ".json", ".webmanifest"}
STAMP = ".artcraft-build.json"


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get(url: str, token: str | None = None, accept: str | None = None, timeout: int = 120,
             retries: int = 3) -> tuple[bytes, dict]:
    headers = {"User-Agent": "artcraft-pages-builder"}
    if accept:
        headers["Accept"] = accept
    # Only ever send the GitHub token to the GitHub API.
    if token and url.startswith(API):
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                return r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
                wait = max(5, int(e.headers.get("X-RateLimit-Reset", time.time() + 60)) - int(time.time()))
                if wait > 900:
                    raise SystemExit("GitHub API rate limit hit; set GITHUB_TOKEN and retry.") from e
                log(f"  rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            if e.code >= 500 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError(f"GET {url} failed")


def gh_json(url: str, token: str | None):
    body, headers = http_get(url, token, accept="application/vnd.github+json")
    m = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", ""))
    return json.loads(body), (m.group(1) if m else None)


def gh_paginate(url: str, token: str | None):
    while url:
        data, url = gh_json(url, token)
        yield from data


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #
def load_catalog(source: str | None) -> list[dict]:
    if not source:
        return []
    try:
        if re.match(r"https?://", source):
            raw, _ = http_get(source, timeout=30)
        else:
            raw = Path(source).read_bytes()
        apps = json.loads(raw).get("apps", [])
        log(f"Catalogue: {len(apps)} apps")
        return apps
    except Exception as e:  # metadata is a nice-to-have
        log(f"warning: could not load catalogue ({e}); using repo names and descriptions")
        return []


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass
class App:
    id: str
    name: str
    tagline: str
    version: str
    tag: str
    published_at: str
    release_url: str
    repo: str
    repo_url: str
    website: str
    color: str
    ink_light: str
    ink_dark: str
    publisher: str
    official: bool
    order: int
    prerelease: bool
    asset_name: str
    asset_url: str
    asset_size: int
    asset_digest: str
    path: str = ""
    icon: str = ""


def pretty_name(slug: str) -> str:
    out = []
    for p in re.split(r"[-_]+", slug):
        m = re.fullmatch(r"([a-z]+)craft", p, re.I)
        out.append(f"{m.group(1).capitalize()}Craft" if m else p.capitalize())
    return " ".join(out)


def color_for(name: str) -> str:
    hue = int(hashlib.sha1(name.encode()).hexdigest()[:4], 16) % 360
    # hsl -> hex so the page can treat every colour the same way
    import colorsys
    r, g, b = colorsys.hls_to_rgb(hue / 360, 0.55, 0.65)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


def find_web_release(full_name: str, repo: dict, catalog: dict, token: str | None,
                     prereleases: bool, scan: int) -> App | None:
    try:
        releases, _ = gh_json(f"{API}/repos/{full_name}/releases?per_page={scan}", token)
    except urllib.error.HTTPError as e:
        log(f"  ! {full_name}: could not list releases ({e.code})")
        return None

    for rel in releases:
        if rel.get("draft") or (rel.get("prerelease") and not prereleases):
            continue
        for asset in rel.get("assets", []):
            m = WEB_ASSET_RE.match(asset["name"])
            if not m:
                continue
            asset_id = m.group("id").lower()
            entry = catalog.get(asset_id) or catalog.get(repo["name"].lower()) or {}
            app_id = entry.get("id", asset_id)
            name = entry.get("name") or pretty_name(app_id)
            color = entry.get("color") or color_for(name)
            return App(
                id=app_id,
                name=name,
                tagline=entry.get("tagline") or (repo.get("description") or "").strip(),
                version=m.group("version"),
                tag=rel["tag_name"],
                published_at=rel.get("published_at") or "",
                release_url=rel["html_url"],
                repo=full_name,
                repo_url=repo["html_url"],
                website=entry.get("website") or "",
                color=color,
                ink_light=entry.get("ink_light") or color,
                ink_dark=entry.get("ink_dark") or color,
                publisher=entry.get("publisher") or "",
                official=entry.get("kind", "official") == "official",
                order=entry.get("_order", 10_000),
                prerelease=bool(rel.get("prerelease")),
                asset_name=asset["name"],
                asset_url=asset["url"],
                asset_size=asset.get("size") or 0,
                asset_digest=asset.get("digest") or "",
            )
    return None


# --------------------------------------------------------------------------- #
# Unpacking
# --------------------------------------------------------------------------- #
def safe_extract(data: bytes, dest: Path) -> None:
    dest = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in PurePosixPath(name).parts:
                raise ValueError(f"unsafe path in zip: {info.filename}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:  # symlink
                continue
            target = (dest / name).resolve()
            if target != dest and dest not in target.parents:
                raise ValueError(f"unsafe path in zip: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def hoist_app_root(app_dir: Path) -> None:
    """Upstream zips hold one top-level folder; move the folder holding the
    shallowest index.html up so the app lives directly in app_dir."""
    found = sorted(app_dir.rglob("index.html"), key=lambda p: len(p.relative_to(app_dir).parts))
    found = [p for p in found if not any(part in CARGO_LEFTOVERS for part in p.relative_to(app_dir).parts)]
    if not found:
        raise FileNotFoundError("no index.html in web zip")
    root = found[0].parent
    if root == app_dir:
        return
    tmp = app_dir.with_name(app_dir.name + ".__tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.move(str(root), str(tmp))
    shutil.rmtree(app_dir)
    tmp.rename(app_dir)


def prune(app_dir: Path) -> int:
    doomed = [app_dir / n for n in HOST_SAMPLES]
    if any((app_dir / m).exists() for m in CARGO_MARKERS):
        doomed += [app_dir / n for n in CARGO_LEFTOVERS]
    doomed += [p for p in app_dir.rglob("*") if p.is_file() and p.suffix in PRECOMPRESSED
               and p.with_suffix("").exists()]
    n = 0
    for p in doomed:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p); n += 1
        elif p.exists() or p.is_symlink():
            p.unlink(); n += 1
    return n


def set_title(text: str, title: str) -> str:
    esc = html.escape(title, quote=False)
    new, n = re.subn(r"<title\b[^>]*>.*?</title\s*>", f"<title>{esc}</title>", text, count=1, flags=re.I | re.S)
    if n:
        return new
    new, n = re.subn(r"(<head\b[^>]*>)", rf"\1\n    <title>{esc}</title>", text, count=1, flags=re.I)
    return new if n else f"<title>{esc}</title>\n{text}"


def relativize_root_paths(app_dir: Path) -> int:
    """Rewrite "/name" -> "./name" for the app's top-level entries. Current
    ArtCraft builds already use relative URLs; this is for builds that don't."""
    entries = sorted((p.name for p in app_dir.iterdir()), key=len, reverse=True)
    if not entries:
        return 0
    pattern = re.compile(r"""(?P<q>["'`(])/(?P<n>(?:%s)(?=[/"'`?#)]))""" % "|".join(map(re.escape, entries)))
    changed = 0
    for f in app_dir.iterdir():
        if f.is_file() and f.suffix.lower() in TEXT_EXTS:
            text = f.read_text(encoding="utf-8", errors="surrogateescape")
            new, n = pattern.subn(r"\g<q>./\g<n>", text)
            if n:
                f.write_text(new, encoding="utf-8", errors="surrogateescape")
                changed += n
    return changed


def favicon_of(app_dir: Path) -> str | None:
    text = (app_dir / "index.html").read_text(encoding="utf-8", errors="replace")
    for tag in re.findall(r"<link\b[^>]*>", text, re.I):
        if re.search(r'rel=["\'][^"\']*icon', tag, re.I):
            m = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
            if m and not re.match(r"(https?:|data:)", m.group(1)):
                rel = re.sub(r"^\.?/", "", m.group(1))
                if (app_dir / rel).is_file():
                    return rel
    return None


def install_app(app: App, out: Path, token: str | None, fix_paths: bool) -> None:
    app_dir = out / app.id
    stamp = app_dir / STAMP
    app.path = f"{app.id}/"
    if stamp.is_file():
        try:
            if json.loads(stamp.read_text()).get("asset_name") == app.asset_name:
                log(f"  = up to date, skipping download")
                return
        except json.JSONDecodeError:
            pass

    log(f"  ↓ {app.asset_name} ({app.asset_size / 1_048_576:.1f} MB)")
    data, _ = http_get(app.asset_url, token, accept="application/octet-stream", timeout=900)
    if app.asset_digest.startswith("sha256:"):
        got = hashlib.sha256(data).hexdigest()
        if got != app.asset_digest.split(":", 1)[1]:
            raise ValueError(f"SHA-256 mismatch for {app.asset_name}")

    if app_dir.exists():
        shutil.rmtree(app_dir)
    app_dir.mkdir(parents=True)
    safe_extract(data, app_dir)
    hoist_app_root(app_dir)
    pruned = prune(app_dir)
    if pruned:
        log(f"    pruned {pruned} unneeded file(s)/folder(s)")

    index = app_dir / "index.html"
    index.write_text(set_title(index.read_text(encoding="utf-8", errors="surrogateescape"),
                               f"{app.name} {app.version}"),
                     encoding="utf-8", errors="surrogateescape")
    if fix_paths and (n := relativize_root_paths(app_dir)):
        log(f"    rewrote {n} root-absolute path(s)")
    stamp.write_text(json.dumps({"asset_name": app.asset_name}))


def fetch_icon(app: App, out: Path, refresh: bool) -> None:
    icons = out / "assets" / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    target = icons / f"{app.id}.webp"
    if target.is_file() and not refresh:
        app.icon = f"assets/icons/{target.name}"
        return
    if app.official:
        try:
            data, headers = http_get(ICON_URL.format(id=app.id), timeout=30, retries=2)
            if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                target.write_bytes(data)
                app.icon = f"assets/icons/{target.name}"
                return
        except Exception:
            pass
    fav = favicon_of(out / app.id)
    if fav:
        app.icon = f"{app.id}/{fav}"


# --------------------------------------------------------------------------- #
# Launcher page
# --------------------------------------------------------------------------- #
PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="theme-color" content="#1e2327">
  <title>__TITLE__</title>
  <meta name="description" content="Web builds of the ArtCraft apps, running entirely in your browser.">
  <link rel="preload" href="assets/fonts/archivo-latin.woff2" as="font" type="font/woff2" crossorigin>
  <script>try{const t=localStorage.getItem("theme");if(t&&t!=="system")document.documentElement.dataset.theme=t}catch(e){}</script>
  <style>
@font-face{font-family:Archivo;src:url("assets/fonts/archivo-latin.woff2") format("woff2");font-weight:100 900;font-stretch:62% 125%;font-display:swap}
@font-face{font-family:"Geist Mono";src:url("assets/fonts/geist-mono-latin.woff2") format("woff2");font-weight:100 900;font-display:swap}
:root{--bg:#f2f1ee;--sunken:#e9e8e4;--ink:#101014;--muted:#656562;--faint:#797975;--line:#cfcec9;
  --sans:Archivo,system-ui,sans-serif;--mono:"Geist Mono",ui-monospace,"DejaVu Sans Mono",monospace;color-scheme:light}
:root[data-theme=dark]{--bg:#111214;--sunken:#08090a;--ink:#f4f3f0;--muted:#a4a5a3;--faint:#959694;--line:#343538;color-scheme:dark}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#111214;--sunken:#08090a;--ink:#f4f3f0;--muted:#a4a5a3;--faint:#959694;--line:#343538;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:400 14px/1.5 var(--sans);-webkit-font-smoothing:antialiased}
a{color:inherit}a:hover{text-decoration:underline}
button,input,select{font:inherit;color:inherit;border:1px solid var(--line);background:transparent;border-radius:0}
button,select{cursor:pointer}
:focus-visible{outline:2px solid var(--ink);outline-offset:4px}
[hidden]{display:none!important}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.hud{font-family:var(--mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase}
.faint{color:var(--faint)}
.wrap{width:100%;max-width:1480px;margin:auto;padding:0 28px}
.topbar{display:flex;align-items:center;min-height:58px;border-bottom:1px solid var(--line);padding:0 20px;gap:20px}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none!important;font-size:22px;font-weight:700;letter-spacing:-.03em;white-space:nowrap;padding-right:20px;border-right:1px solid var(--line);min-height:58px}
.suite{color:var(--muted);white-space:nowrap}
.topbar-end{display:flex;align-items:center;gap:16px;margin-left:auto}
.segments{display:flex;border-left:1px solid var(--line);padding-left:12px}
.segments button{border:0;padding:5px 8px;font-family:var(--mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase}
.segments button[aria-pressed=true]{background:var(--ink);color:var(--bg)}
#theme{padding:7px;font-size:12px;max-width:142px;background:var(--bg)}
.toolbar{display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:20px 0 8px}
.search-sort{display:flex;gap:10px;align-items:center;margin-left:auto;flex-wrap:wrap}
.search-sort label{display:flex;align-items:center;gap:8px}
#sort{padding:8px 10px;background:var(--bg)}
#search{padding:8px 12px;width:210px;background:var(--sunken)}
#apps{margin:18px 0 30px}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border-top:1px solid var(--line);border-left:1px solid var(--line)}
@media (max-width:1100px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:700px){.grid{grid-template-columns:1fr}.suite{display:none}.wrap{padding:0 16px}.topbar{flex-wrap:wrap;padding:8px 16px}.brand{border:0;min-height:0;font-size:19px}.search-sort{width:100%}.search-sort label:last-child{flex:1}#search{width:100%}.card-body{min-height:0}.version{margin-bottom:18px}}
.card{--app:#808080;display:flex;flex-direction:column;min-width:0;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}
.tint{--app-ink:var(--app-light)}
:root[data-theme=dark] .tint{--app-ink:var(--app-dark)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .tint{--app-ink:var(--app-dark)}}
.card-head{min-height:40px;padding:8px 19px;display:flex;justify-content:space-between;align-items:center;gap:10px;border-bottom:1px solid var(--line)}
.number{background:var(--app);color:#08090a;font:700 11px var(--mono);letter-spacing:.14em;padding:4px 7px}
.links{display:flex;gap:13px;font:10px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.links a{text-decoration:none}.links a:hover{color:var(--ink)}
.card-body{display:flex;flex-direction:column;flex:1;padding:20px 19px 19px;min-height:235px}
.identity{display:flex;align-items:flex-start;gap:14px;min-width:0}
.logo{flex:none;width:56px;height:56px;object-fit:cover;border-radius:12px}
.monogram{display:flex;align-items:center;justify-content:center;background:var(--app);color:#08090a;font-size:28px;font-weight:700}
.name{margin:0;font-weight:680;font-stretch:110%;letter-spacing:-.025em;font-size:23px;line-height:1.2}
.name a{text-decoration:none}
.accent{color:var(--app-ink)}
.tagline{color:var(--muted);margin:7px 0 0;font-size:14px;line-height:1.45}
.byline{margin:6px 0 0;font:400 10px var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.pill{display:inline-block;border:1px solid var(--line);padding:1px 5px;font:10px var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--app-ink);margin-left:6px;vertical-align:1px}
.version{color:var(--muted);margin:24px 0 15px;font-size:13px}
.actions{display:flex;gap:8px;margin-top:auto}
.open,.more{display:flex;align-items:center;justify-content:center;min-height:36px;border:1px solid var(--muted);text-decoration:none!important;font:700 11px var(--mono);letter-spacing:.15em;text-transform:uppercase;padding:8px 16px}
.open{flex:1}.more{padding:8px 12px;font-weight:400;color:var(--muted)}
.open:hover,.more:hover{background:var(--sunken);border-color:var(--ink);color:var(--ink)}
.table-scroll{overflow-x:auto;border:1px solid var(--line)}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:middle}
th{font:400 10px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--faint);font-weight:400}
tr:last-child td{border-bottom:0}
td .identity{align-items:center}td .logo{width:36px;height:36px;border-radius:8px}td .monogram{font-size:18px}
td .name{font-size:17px}td .open{min-height:30px;padding:5px 14px}
td.num{width:1%}td.links-cell .links{gap:10px}
.empty{color:var(--muted);padding:40px 0}
.footer{border-top:1px solid var(--line);padding:22px 28px 40px;color:var(--muted);font-size:13px}
.footer p{margin:0 0 6px}
@media (prefers-reduced-motion:no-preference){.card{transition:background .15s}.card:hover{background:color-mix(in srgb,var(--app) 5%,transparent)}}
  </style>
</head>
<body>
  <header class="topbar">
    <a class="brand" href="./">__TITLE__</a>
    <span class="suite hud">Craft App Suite</span>
    <div class="topbar-end">
      <div class="segments" role="group" aria-label="View">
        <button type="button" data-view="grid" aria-pressed="true">Grid</button>
        <button type="button" data-view="table" aria-pressed="false">Table</button>
      </div>
      <label><span class="sr-only">Theme</span><select id="theme"><option value="system">Match system</option><option value="dark">Dark</option><option value="light">Light</option></select></label>
    </div>
  </header>
  <main class="wrap">
    <div class="toolbar">
      <span id="count" class="hud faint" aria-live="polite"></span>
      <div class="search-sort">
        <label><span class="hud faint">Sort</span><select id="sort"><option value="suite">Suite order</option><option value="name">Name</option><option value="newest">Newest release</option></select></label>
        <label><span class="sr-only">Search apps</span><input id="search" type="search" placeholder="Search apps" autocomplete="off"></label>
      </div>
    </div>
    <div id="apps">
      <noscript><ul>__NOSCRIPT__</ul></noscript>
    </div>
  </main>
  <footer class="footer">
    <p class="hud faint">Built __BUILT__ · <a href="apps.json">apps.json</a></p>
    <p>Each app runs entirely in your browser. Documents and libraries stay in this browser for this site; export or back them up from inside each app.</p>
    <p class="faint">Apps and logos by <a href="https://getartcraft.com/apps" rel="noopener">ArtCraft</a> (Apache-2.0 / MIT). Layout after <a href="https://github.com/AurelioB/artcraft-web-launcher" rel="noopener">ArtCraft Web Launcher</a>. Fonts: Archivo and Geist Mono (SIL OFL).</p>
  </footer>
  <script id="data" type="application/json">__DATA__</script>
  <script>
(() => {
  const apps = JSON.parse(document.getElementById("data").textContent);
  const $ = (s) => document.querySelector(s);
  const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } },
                  set(k, v) { try { localStorage.setItem(k, v); } catch {} } };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const date = (iso) => iso ? new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "";
  const vars = (a) => `--app:${esc(a.color)};--app-light:${esc(a.ink_light)};--app-dark:${esc(a.ink_dark)}`;
  const name = (a) => { const m = /^(.+?)(Craft)$/.exec(a.name);
    return m ? `${esc(m[1])}<span class="accent">${m[2]}</span>` : esc(a.name); };
  const logo = (a) => a.icon
    ? `<img class="logo" src="${esc(a.icon)}" alt="" width="56" height="56" loading="lazy" onerror="this.outerHTML='<span class=&quot;logo monogram&quot; aria-hidden=&quot;true&quot;>${esc(a.name[0])}</span>'">`
    : `<span class="logo monogram" aria-hidden="true">${esc(a.name[0])}</span>`;
  const links = (a) => `<span class="links">${a.website ? `<a href="${esc(a.website)}" rel="noopener">Website ↗</a>` : ""}<a href="${esc(a.repo_url)}" rel="noopener">GitHub ↗</a></span>`;
  const pre = (a) => a.prerelease ? `<span class="pill">pre-release</span>` : "";
  const byline = (a) => !a.official && a.publisher ? `<p class="byline">by ${esc(a.publisher)}</p>` : "";

  const card = (a) => `
    <article class="card tint" style="${vars(a)}">
      <div class="card-head"><span class="number">${a.num}</span>${links(a)}</div>
      <div class="card-body">
        <div class="identity">${logo(a)}<div>
          <h2 class="name"><a href="${esc(a.path)}">${name(a)}</a>${pre(a)}</h2>
          ${a.tagline ? `<p class="tagline">${esc(a.tagline)}</p>` : ""}${byline(a)}
        </div></div>
        <p class="version">Version ${esc(a.version)}${a.published_at ? " · " + date(a.published_at) : ""}</p>
        <div class="actions">
          <a class="open" href="${esc(a.path)}">Open</a>
          <a class="more" href="${esc(a.release_url)}" rel="noopener" title="Release notes">Notes</a>
        </div>
      </div>
    </article>`;

  const row = (a) => `
    <tr class="tint" style="${vars(a)}">
      <td class="num"><span class="number">${a.num}</span></td>
      <td><div class="identity">${logo(a)}<div><div class="name"><a href="${esc(a.path)}">${name(a)}</a>${pre(a)}</div>${byline(a)}</div></div></td>
      <td>${esc(a.tagline)}</td>
      <td>${esc(a.version)}</td>
      <td>${date(a.published_at)}</td>
      <td class="links-cell">${links(a)}</td>
      <td><a class="open" href="${esc(a.path)}">Open</a></td>
    </tr>`;

  let view = store.get("view") === "table" ? "table" : "grid";
  const sortSel = $("#sort"), search = $("#search"), theme = $("#theme");
  sortSel.value = store.get("sort") || "suite";
  theme.value = store.get("theme") || "system";

  function render() {
    const q = search.value.trim().toLowerCase();
    let list = apps.filter((a) => !q || [a.name, a.tagline, a.repo, a.publisher].join(" ").toLowerCase().includes(q));
    const by = sortSel.value;
    list = list.slice().sort((x, y) =>
      by === "name" ? x.name.localeCompare(y.name)
      : by === "newest" ? (y.published_at || "").localeCompare(x.published_at || "")
      : x.idx - y.idx);
    $("#count").textContent = q ? `${list.length} of ${apps.length} apps` : `${apps.length} apps`;
    document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.view === view));
    const box = $("#apps");
    if (!list.length) { box.innerHTML = `<p class="empty">${apps.length ? "No apps match your search." : "No web builds found yet."}</p>`; return; }
    box.innerHTML = view === "grid"
      ? `<div class="grid">${list.map(card).join("")}</div>`
      : `<div class="table-scroll"><table><thead><tr><th>#</th><th>App</th><th>Description</th><th>Version</th><th>Released</th><th>Links</th><th></th></tr></thead><tbody>${list.map(row).join("")}</tbody></table></div>`;
  }

  document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => { view = b.dataset.view; store.set("view", view); render(); }));
  sortSel.addEventListener("change", () => { store.set("sort", sortSel.value); render(); });
  search.addEventListener("input", render);
  theme.addEventListener("change", () => {
    store.set("theme", theme.value);
    if (theme.value === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme.value;
  });
  render();
})();
  </script>
</body>
</html>
"""


def render_index(apps: list[App], title: str) -> str:
    data = []
    for i, a in enumerate(apps):
        d = asdict(a)
        for k in ("asset_url", "asset_digest", "order"):
            d.pop(k)
        d["idx"] = i
        d["num"] = f"{i + 1:02d}"
        data.append(d)
    blob = json.dumps(data).replace("</", "<\\/")
    noscript = "".join(f'<li><a href="{html.escape(a.path)}">{html.escape(a.name)} {html.escape(a.version)}</a></li>'
                       for a in apps)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (PAGE.replace("__TITLE__", html.escape(title)).replace("__BUILT__", built)
            .replace("__NOSCRIPT__", noscript).replace("__DATA__", blob))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", default=os.environ.get("ARTCRAFT_ORG", "storytold"))
    ap.add_argument("--out", default="site", type=Path)
    ap.add_argument("--title", default="ArtCraft Web Apps")
    ap.add_argument("--catalog", default=DEFAULT_CATALOG_URL,
                    help="catalogue URL or file for names/colours; '' to disable")
    ap.add_argument("--extra-repo", nargs="*", default=[], metavar="OWNER/REPO",
                    help="more repos to check (catalogue community apps are added automatically)")
    ap.add_argument("--include", nargs="*", default=[], help="only these app ids or repo names")
    ap.add_argument("--exclude", nargs="*", default=[], help="skip these app ids or repo names")
    ap.add_argument("--prereleases", action="store_true", help="also consider pre-releases")
    ap.add_argument("--scan", type=int, default=10, help="recent releases to scan per repo")
    ap.add_argument("--fix-paths", action="store_true", help="rewrite '/root' asset paths to './relative'")
    ap.add_argument("--refresh-icons", action="store_true")
    ap.add_argument("--clean", action="store_true", help="delete the output folder first")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log("warning: no GITHUB_TOKEN; unauthenticated GitHub API limit is 60 requests/hour")

    out: Path = args.out
    if args.clean and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    # Catalogue lookup by id and former ids, remembering suite order.
    catalog: dict[str, dict] = {}
    cat_repos: list[str] = []
    for i, entry in enumerate(load_catalog(args.catalog)):
        entry["_order"] = i
        for key in [entry.get("id", "")] + list(entry.get("former_ids") or []):
            if key:
                catalog[key.lower()] = entry
        if entry.get("repository"):
            cat_repos.append(entry["repository"])

    log(f"Listing repos in {args.org}…")
    repos: dict[str, dict] = {}
    for r in gh_paginate(f"{API}/orgs/{args.org}/repos?per_page=100&type=public", token):
        if not r.get("archived") and not r.get("fork"):
            repos[r["full_name"].lower()] = r
    for full in cat_repos + args.extra_repo:
        if full.lower() in repos:
            continue
        try:
            r, _ = gh_json(f"{API}/repos/{full}", token)
            repos[r["full_name"].lower()] = r
        except urllib.error.HTTPError as e:
            log(f"  ! {full}: {e.code}")
    log(f"{len(repos)} repos to check")

    wanted = lambda *keys: (not args.include or any(k in args.include for k in keys)) and \
        not any(k in args.exclude for k in keys)

    apps: dict[str, App] = {}
    failures: list[str] = []
    for full, repo in sorted(repos.items()):
        if not wanted(repo["name"]):
            continue
        app = find_web_release(repo["full_name"], repo, catalog, token, args.prereleases, args.scan)
        if not app or not wanted(app.id, repo["name"]):
            continue
        if app.id in apps:
            log(f"  ! {repo['full_name']}: id '{app.id}' already used by {apps[app.id].repo}, skipping")
            continue
        log(f"• {app.name} {app.version}  ({repo['full_name']} {app.tag})")
        try:
            install_app(app, out, token, args.fix_paths)
            fetch_icon(app, out, args.refresh_icons)
            apps[app.id] = app
        except Exception as e:  # one broken release shouldn't take down the site
            log(f"  ! failed: {e}")
            failures.append(f"{repo['full_name']}: {e}")
            shutil.rmtree(out / app.id, ignore_errors=True)

    for d in out.iterdir():
        if d.is_dir() and (d / STAMP).exists() and d.name not in apps:
            log(f"- removing {d.name}/ (no longer published)")
            shutil.rmtree(d)

    fonts_src = ROOT / "assets" / "fonts"
    if fonts_src.is_dir():
        shutil.copytree(fonts_src, out / "assets" / "fonts", dirs_exist_ok=True)

    ordered = sorted(apps.values(), key=lambda a: (a.order, a.name.lower()))
    (out / "index.html").write_text(render_index(ordered, args.title), encoding="utf-8")
    (out / "apps.json").write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apps": [{k: getattr(a, k) for k in ("id", "name", "version", "tag", "published_at", "repo",
                                             "release_url", "asset_name", "asset_digest", "path")}
                 for a in ordered]}, indent=2))
    (out / ".nojekyll").touch()

    log(f"\nBuilt {len(ordered)} app(s) into {out}/")
    if failures:
        log("Failures:\n  " + "\n  ".join(failures))
    return 1 if failures and not ordered else 0


if __name__ == "__main__":
    sys.exit(main())

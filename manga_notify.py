#!/usr/bin/env python3
"""mal-notify (manga) — même architecture que la version anime, sans Jikan.

Type / chapters / volumes viennent de la table de résultats de la page de
recherche (rendue côté serveur) ; titre/synopsis/image de la fiche.
État indépendant : state_manga.json.
Webhook : WEBHOOK_MANGA_URL si défini, sinon WEBHOOK_URL (salon animes).
"""

import html as html_lib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ réglages
MANGA_PAGE = "https://myanimelist.net/manga.php?o=9&c%5B0%5D=a&c%5B1%5D=b&c%5B2%5D=c&cv=3&w=1"
MANGA_BASE = "https://myanimelist.net/manga/"
WEBHOOK = os.environ.get("WEBHOOK_MANGA_URL") or os.environ["WEBHOOK_URL"]

STATE_FILE = Path("state_manga.json")
SHOW_OFFSETS = [0, 20, 40]

MAX_NOTIFS = 10
MAX_FETCHES = 15
WATCH_ROTATION = 12
WATCH_TTL_DAYS = 60
WATCH_ALERT_SIZE = 60
ALERT_AFTER = 20

STATUTS = ("Not yet published", "Publishing", "Discontinued", "On Hiatus", "Finished")
TYPES   = ("Light Novel", "One-shot", "Manhwa", "Manhua", "Novel", "Manga", "Doujinshi")

PENDING_HINTS = [
    "pending approval",
    "not yet been approved",
    "has not been approved",
    "awaiting approval",
    "pending addition",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# ------------------------------------------------------------------ état
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"notified": [], "watch": {}, "failures": 0, "alerted": False}

def save_state(state):
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, indent=1))

# ------------------------------------------------------------------ réseau
def _fetch_cffi(url):
    from curl_cffi import requests as cffi
    r = cffi.get(url, impersonate="chrome", timeout=30)
    return r.text, r.status_code

def _fetch_scraper(url):
    import cloudscraper
    r = cloudscraper.create_scraper().get(url, timeout=30)
    return r.text, r.status_code

def _fetch_plain(url):
    r = requests.get(url, timeout=30, headers={"User-Agent": UA})
    return r.text, r.status_code

def get_page(url):
    """(html, code HTTP) via curl_cffi / cloudscraper / requests, en cascade."""
    last = "aucune méthode n'a abouti"
    for fetch in (_fetch_cffi, _fetch_scraper, _fetch_plain):
        try:
            text, code = fetch(url)
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue
        if "just a moment" in text.lower() or code in (403, 503, 429):
            last = f"HTTP {code} / challenge Cloudflare"
            continue
        return text, code
    raise RuntimeError(last)

def scrape_window():
    """IDs + infos de ligne (type, chapters, volumes) de la table des résultats,
    rendue côté serveur. Renvoie (ids, infos) — ids du plus récent au plus
    ancien, sans doublon ; infos = {id: {type, ch, vol}}."""
    rows, sample = [], ""
    for show in SHOW_OFFSETS:
        url = MANGA_PAGE if show == 0 else f"{MANGA_PAGE}&show={show}"
        text, code = get_page(url)
        if code != 200:
            raise RuntimeError(f"page de recherche illisible (HTTP {code})")
        content = text.split('id="content"', 1)[-1]
        soup = BeautifulSoup(content, "html.parser")
        for tr in soup.find_all("tr"):
            a = tr.find("a", href=re.compile(r"/manga/\d+/"))
            if not a:
                continue
            m = re.search(r"/manga/(\d+)/", a["href"])
            if not m:
                continue
            row = {"id": m.group(1)}
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            for i, td in enumerate(tds):
                if td in TYPES:                      # cellule Type de MAL
                    row["type"] = td
                    ch  = tds[i + 1] if i + 1 < len(tds) else ""   # chapters
                    vol = tds[i + 2] if i + 2 < len(tds) else ""   # volumes
                    row["ch"]  = ch  if re.fullmatch(r"\d+", ch)  else "?"
                    row["vol"] = vol if re.fullmatch(r"\d+", vol) else "?"
                    break
            if "type" not in row:                   # secours regex sur la ligne
                toks = re.findall(r">\s*(Light Novel|One-shot|Manhwa|Manhua|"
                                  r"Novel|Manga|Doujinshi)\s*<", str(tr))
                if toks:
                    row["type"] = toks[-1]          # la cellule Type vient après le titre
            if "type" not in row and not sample:
                sample = str(tr)[:500]
            rows.append(row)
        time.sleep(1.2)
    if rows and not any("type" in r for r in rows) and sample:
        print("  [diag table] aucun type trouvé dans les résultats — extrait d'une ligne :")
        print(f"  [diag table] {sample}")
    ids = list(dict.fromkeys(r["id"] for r in rows))
    infos = {}
    for r in rows:
        infos.setdefault(r["id"], r)
    return ids, infos

# ------------------------------------------------------------------ parsing
def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def parse_details(text, mal_id):
    """Titre / synopsis / image / statut depuis la fiche (multi-stratégies).
    Type / chapters / volumes viennent de la table de recherche."""

    soup = BeautifulSoup(text, "html.parser")
    det = {"mal_id": mal_id, "title": "", "type": "?", "status": "?",
           "volumes": "?", "chapters": "?", "synopsis": "", "image": ""}

    # ------------------------------------------------------------------ titre
    m = (re.search(r'property=["\']og:title["\']\s+content=["\']([^"\']+)', text)
         or re.search(r'content=["\']([^"\']+)["\']\s+property=["\']og:title["\']', text))
    if m:
        t = html_lib.unescape(m.group(1))
        t = re.sub(r"\s*[-–—|]\s*MyAnimeList(\.net)?\s*$", "", t)
        det["title"] = _clean(t)
    if not det["title"]:
        h1 = soup.select_one("h1.title-name") or soup.select_one("h1")
        if h1:
            parts = []
            for s in h1.stripped_strings:
                if s.lower().rstrip(":") == "edit" or s.lower().startswith("what would you like"):
                    break
                parts.append(s)
            det["title"] = _clean(" ".join(parts))
    if not det["title"]:
        m = re.search(r"<title>(.*?)</title>", text, re.S)
        if m:
            t = html_lib.unescape(m.group(1))
            t = re.sub(r"\s*[-–—|]\s*MyAnimeList(\.net)?\s*$", "", t)
            det["title"] = _clean(t)

    # --------------------------------------------------------------- statut
    m = re.search(r"Status:\s*(?:</[a-z]+>|<[^>]*>)?\s*([^<]+)", text)
    if m:
        v = _clean(html_lib.unescape(m.group(1)))
        if "${" not in v:
            det["status"] = v or "?"
    if det["status"] == "?":
        det["status"] = next((s for s in STATUTS if s in text), "?")

    # -------------------------------------------------------------- synopsis
    p = (soup.select_one('p[itemprop="description"]')
         or soup.select_one('span[itemprop="description"]'))
    if p:
        det["synopsis"] = (_clean(p.get_text())
                           .replace("[Written by MAL Rewrite]", "").strip())
    if not det["synopsis"]:
        m = (re.search(r'property=["\']og:description["\']\s+content=["\']([^"\']+)', text)
             or re.search(r'content=["\']([^"\']+)["\']\s+property=["\']og:description["\']', text))
        if m:
            s = html_lib.unescape(m.group(1)).replace("\r", " ").replace("\n", " ")
            det["synopsis"] = _clean(s.replace("**", ""))

    # ----------------------------------------------------------------- image
    m = (re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)', text)
         or re.search(r'content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', text))
    if m:
        det["image"] = m.group(1)

    # ------------------------------------------- diagnostic auto si besoin
    if not det["title"]:
        print(f"  [diag #{mal_id}] titre introuvable")
        i = text.find("og:title")
        if i >= 0:
            print(f"  [diag] {text[max(0, i-40):i+260].replace(chr(10), ' ')[:300]}")
    return det

def fetch_entry(mal_id):
    """Classe une entrée : 'approved' (+ détails), 'pending', 'missing', 'error'."""
    try:
        text, code = get_page(f"{MANGA_BASE}{mal_id}")
    except Exception as exc:
        return "error", {"error": f"{type(exc).__name__}: {exc}"}
    low = text.lower()
    if code == 404 or "no such entry" in low or 'class="badresult"' in low:
        return "missing", {}
    hint = next((h for h in PENDING_HINTS if h in low), None)
    if hint:
        return "pending", {"hint": hint}
    return "approved", parse_details(text, mal_id)

# ------------------------------------------------------------------ discord
def send_embed(embed):
    for _ in range(3):
        try:
            r = requests.post(WEBHOOK, json={"embeds": [embed]}, timeout=15)
        except Exception:
            time.sleep(2)
            continue
        if r.status_code in (200, 204):
            return True
        time.sleep(float(r.json().get("retry_after", 2)) if r.status_code == 429 else 2)
    print(f"!! échec d'envoi Discord : {embed.get('title')}")
    return False

def announce(det, row=None):
    row = row or {}
    typ = det.get("type") or "?"
    if typ == "?":
        typ = row.get("type") or "?"          # type de la table de recherche
    statut = det.get("status") or "?"
    ch = det.get("chapters") or "?"
    if ch == "?":
        ch = row.get("ch") or "?"
    vol = det.get("volumes") or "?"
    if vol == "?":
        vol = row.get("vol") or "?"
    embed = {
        "title": det.get("title") or f"Nouvelle entrée #{det['mal_id']}",
        "url": f"{MANGA_BASE}{det['mal_id']}",
        "color": 0x8E44AD,                       # violet manga
        "footer": {"text": f"MANGA #{det['mal_id']}"},
    }
    if det.get("synopsis"):
        s = det["synopsis"]
        if len(s) > 500:
            s = s[:500].rsplit(" ", 1)[0] + "…"
        embed["description"] = s
    embed["fields"] = [
        {"name": "Type",     "value": typ,    "inline": True},
        {"name": "Statut",   "value": statut, "inline": True},
        {"name": "Chapter" if ch == "1" else "Chapters", "value": ch, "inline": True},
        {"name": "Volume" if vol == "1" else "Volumes",  "value": vol, "inline": True},
    ]
    if det.get("image"):
        embed["image"] = {"url": det["image"]}
    ok = send_embed(embed)
    if ok:
        print(f"  -> notifié : #{det['mal_id']} — {embed['title']} (type : {typ})")
    return ok

# ------------------------------------------------------------------ main
def main():
    state = load_state()

    # 1) fenêtre des nouvelles entrées --------------------------------------
    try:
        window, infos = scrape_window()
        if len(window) < 10:
            raise RuntimeError(f"seulement {len(window)} entrées lues (parsing cassé ?)")
    except Exception as exc:
        state["failures"] = state.get("failures", 0) + 1
        print(f"échec de lecture de MAL ({state['failures']} d'affilée) : {exc}")
        if state["failures"] >= ALERT_AFTER and not state.get("alerted"):
            send_embed({"title": "⚠️ mal-notify (manga) n'arrive plus à lire MAL",
                        "description": f"{state['failures']} tentatives ratées d'affilée. "
                                       "Vérifie les logs dans l'onglet Actions du repo.",
                        "color": 0xE67E22})
            state["alerted"] = True
        save_state(state)
        return

    state["failures"] = 0
    state["alerted"] = False
    print(f"page MAL (manga) lue : {len(window)} entrées dans la fenêtre")

    notified = set(state["notified"])
    watch = state["watch"]
    now = datetime.now(timezone.utc)

    # 2) tout premier lancement : mémoriser l'existant sans rien envoyer ----
    if not notified and not watch:
        state["notified"] = sorted(set(window), key=int)
        save_state(state)
        print(f"initialisé silencieusement sur {len(window)} entrées")
        return

    # 3) purge : entrées en attente depuis trop longtemps --------------------
    for mal_id, info in list(watch.items()):
        try:
            age = (now - datetime.fromisoformat(info["since"])).days
        except Exception:
            age = 999
        if age > WATCH_TTL_DAYS:
            print(f"#{mal_id} jamais validée depuis {age} j -> abandon")
            del watch[mal_id]
            notified.add(mal_id)

    # 4) file d'attente anormalement grande ? --------------------------------
    if len(watch) > WATCH_ALERT_SIZE and not state.get("watch_alerted"):
        send_embed({"title": "⚠️ mal-notify (manga) : file d'attente suspicieusement grande",
                    "description": f"{len(watch)} entrées bloquées en attente. Vérifie "
                                   "les logs (onglet Actions) du repo.",
                    "color": 0xE67E22})
        state["watch_alerted"] = True
    elif len(watch) < 30:
        state["watch_alerted"] = False

    # 5) examen : nouveaux IDs d'abord, puis rotation de la file -------------
    fresh = [i for i in window if i not in notified and i not in watch]
    print(f"{len(fresh)} nouvel(s) ID, {len(watch)} en file d'attente")

    queue = list(reversed(fresh))
    queue += sorted(watch, key=lambda i: watch[i].get("since", ""))[:WATCH_ROTATION]

    fetches = sent = 0
    for mal_id in queue:
        if fetches >= MAX_FETCHES or sent >= MAX_NOTIFS:
            break
        fetches += 1
        time.sleep(1.4)
        row = infos.get(mal_id) or watch.get(mal_id, {}).get("row") or {}
        classe, det = fetch_entry(mal_id)
        if classe == "approved":
            if announce(det, row):
                notified.add(mal_id)
                watch.pop(mal_id, None)
                sent += 1
        elif classe == "error":
            print(f"  #{mal_id} : lecture impossible ({det.get('error', '?')})")
            info = watch.setdefault(mal_id, {"since": now.isoformat(), "tries": 0, "row": row})
            info["since"] = now.isoformat()
        else:
            info = watch.setdefault(mal_id, {"since": now.isoformat(), "tries": 0, "row": row})
            info["tries"] += 1
            raison = ("page absente" if classe == "missing"
                      else f"en attente de validation ({det.get('hint')})")
            print(f"  #{mal_id} : {raison} — essai {info['tries']}")

    # 6) sauvegarde -----------------------------------------------------------
    state["notified"] = sorted(notified, key=int)[-2000:]
    state["watch"] = watch
    save_state(state)
    print(f"terminé : {sent} notification(s) envoyée(s), {len(watch)} en file")

if __name__ == "__main__":
    main()

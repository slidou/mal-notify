#!/usr/bin/env python3
"""mal-notify v2 — plus aucune dépendance à Jikan : tout est lu sur MAL en direct.

  1. la page « nouvelles entrées » est scrapée comme avant (curl_cffi & co) ;
  2. pour chaque ID inconnu, la fiche myanimelist.net/anime/<ID> est chargée
     elle aussi directement ;
  3. fiche introuvable (404) ou marquée « pending approval » -> file d'attente,
     re-vérifiée en rotation à chaque run ;
  4. fiche normale -> notif Discord (titre, type, statut, synopsis, image
     extraits de la page elle-même).
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
MAL_PAGE = "https://myanimelist.net/anime.php?o=9&c%5B0%5D=a&c%5B1%5D=d&cv=2&w=1"
MAL_ANIME = "https://myanimelist.net/anime/"
WEBHOOK = os.environ["WEBHOOK_URL"]

STATE_FILE = Path("state.json")
SHOW_OFFSETS = [0, 20, 40]

MAX_NOTIFS = 10        # embeds max envoyés par run
MAX_FETCHES = 15       # fiches MAL max consultées par run
WATCH_ROTATION = 12    # entrées de la file re-vérifiées par run
WATCH_TTL_DAYS = 60    # abandon des entrées jamais validées
WATCH_ALERT_SIZE = 60  # alerte Discord si la file dépasse cette taille
ALERT_AFTER = 20       # runs d'affilée sans lecture de MAL avant alerte

# formulations signalant une entrée pas encore validée (en minuscules)
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
    """(html, code HTTP) via curl_cffi / cloudscraper / requests, en cascade.
    Lève RuntimeError si les trois méthodes sont bloquées."""
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
    """IDs visibles sur la page des nouvelles entrées, du plus récent au plus ancien."""
    ids = []
    for show in SHOW_OFFSETS:
        url = MAL_PAGE if show == 0 else f"{MAL_PAGE}&show={show}"
        text, code = get_page(url)
        if code != 200:
            raise RuntimeError(f"page de recherche illisible (HTTP {code})")
        text = text.split('id="content"', 1)[-1]
        ids += re.findall(r'href="[^"]*/anime/(\d+)/', text)
        time.sleep(1.2)
    return list(dict.fromkeys(ids))

# ------------------------------------------------------------------ parsing
def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def parse_details(text, mal_id):
    """Titre / type / statut / synopsis / image — multi-stratégies :
    meta og: (stables), motifs texte insensibles aux classes CSS, secours.
    En cas de champ manquant, un [diag] imprime l'extrait HTML exact
    dans les logs (pour une correction factuelle, pas à l'aveugle)."""

    soup = BeautifulSoup(text, "html.parser")
    det = {"mal_id": mal_id, "title": "", "type": "?", "status": "?",
           "volumes": "", "chapters": "", "synopsis": "", "image": ""}

    # ------------------------------------------------------------------ titre
    m = (re.search(r'property=["\']og:title["\']\s+content=["\']([^"\']+)', text)
         or re.search(r'content=["\']([^"\']+)["\']\s+property=["\']og:title["\']', text))
    if m:                                    # 1) meta og:title
        t = html_lib.unescape(m.group(1))
        t = re.sub(r"\s*[-–—|]\s*MyAnimeList(\.net)?\s*$", "", t)
        det["title"] = _clean(t)
    if not det["title"]:                     # 2) h1, nettoyé du menu d'édition
        h1 = soup.select_one("h1.title-name") or soup.select_one("h1")
        if h1:
            parts = []
            for s in h1.stripped_strings:
                if s.lower().rstrip(":") == "edit" or s.lower().startswith("what would you like"):
                    break
                parts.append(s)
            det["title"] = _clean(" ".join(parts))
    if not det["title"]:                     # 3) <title> de la page
        m = re.search(r"<title>(.*?)</title>", text, re.S)
        if m:
            t = html_lib.unescape(m.group(1))
            t = re.sub(r"\s*[-–—|]\s*MyAnimeList(\.net)?\s*$", "", t)
            det["title"] = _clean(t)

    # ------------------------------------------- type / statut / vol. / ch.
    # motif indépendant des classes CSS : « Libellé:</balise éventuelle> valeur »
    for label, field in (("Type", "type"), ("Status", "status"),
                         ("Volumes", "volumes"), ("Chapters", "chapters")):
        m = re.search(label + r":\s*(?:</[a-z]+>|<[^>]*>)?\s*([^<]+)", text)
        if m:
            det[field] = _clean(html_lib.unescape(m.group(1)))

    # -------------------------------------------------------------- synopsis
    p = (soup.select_one('p[itemprop="description"]')
         or soup.select_one('span[itemprop="description"]'))
    if p:
        det["synopsis"] = (_clean(p.get_text())
                           .replace("[Written by MAL Rewrite]", "").strip())
    if not det["synopsis"]:                  # secours : meta og:description
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
    manquants = [k for k, v in (("titre", det["title"]), ("type", det["type"]),
                                ("statut", det["status"])) if not v or v == "?"]
    if manquants:
        print(f"  [diag #{mal_id}] non trouvés : {', '.join(manquants)}")
        for marqueur in ('og:title', "<h1", "Type:", "Status:"):
            i = text.find(marqueur)
            if i >= 0:
                extrait = text[max(0, i - 40):i + 260].replace("\n", " ")
                print(f"  [diag] {extrait[:300]}")
    return det

def fetch_entry(mal_id):
    """Classe une entrée : 'approved' (+ détails), 'pending', 'missing', 'error'."""
    try:
        text, code = get_page(f"{MAL_ANIME}{mal_id}")
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

def announce(det):
    embed = {
        "title": det.get("title") or f"Nouvelle entrée #{det['mal_id']}",
        "url": f"{MAL_ANIME}{det['mal_id']}",
        "color": 0x2E51A2,
        "fields": [
            {"name": "Type",   "value": det.get("type")   or "?", "inline": True},
            {"name": "Statut", "value": det.get("status") or "?", "inline": True},
        ],
        "footer": {"text": f"MAL #{det['mal_id']}"},
    }
    if det.get("synopsis"):
        embed["description"] = det["synopsis"][:300]
    if det.get("image"):
        embed["thumbnail"] = {"url": det["image"]}
    ok = send_embed(embed)
    if ok:
        print(f"  -> notifié : #{det['mal_id']} — {embed['title']}")
    return ok

# ------------------------------------------------------------------ main
def main():
    state = load_state()

    # 1) fenêtre des nouvelles entrées --------------------------------------
    try:
        window = scrape_window()
        if len(window) < 10:
            raise RuntimeError(f"seulement {len(window)} entrées lues (parsing cassé ?)")
    except Exception as exc:
        state["failures"] = state.get("failures", 0) + 1
        print(f"échec de lecture de MAL ({state['failures']} d'affilée) : {exc}")
        if state["failures"] >= ALERT_AFTER and not state.get("alerted"):
            send_embed({"title": "⚠️ mal-notify n'arrive plus à lire MAL",
                        "description": f"{state['failures']} tentatives ratées d'affilée. "
                                       "Vérifie les logs dans l'onglet Actions du repo.",
                        "color": 0xE67E22})
            state["alerted"] = True
        save_state(state)
        return

    state["failures"] = 0
    state["alerted"] = False
    print(f"page MAL lue : {len(window)} entrées dans la fenêtre")

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

    # 4) file d'attente anormalement grande ? (le piège à silence de Jikan) --
    if len(watch) > WATCH_ALERT_SIZE and not state.get("watch_alerted"):
        send_embed({"title": "⚠️ mal-notify : file d'attente suspicieusement grande",
                    "description": f"{len(watch)} entrées bloquées en attente. Vérifie "
                                   "les logs (onglet Actions) : MAL refuse peut-être "
                                   "ses pages au bot.",
                    "color": 0xE67E22})
        state["watch_alerted"] = True
    elif len(watch) < 30:
        state["watch_alerted"] = False

    # 5) examen : nouveaux IDs d'abord, puis rotation de la file -------------
    fresh = [i for i in window if i not in notified and i not in watch]
    print(f"{len(fresh)} nouvel(s) ID, {len(watch)} en file d'attente")

    queue = list(reversed(fresh))                                    # ancien -> récent
    queue += sorted(watch, key=lambda i: watch[i].get("since", ""))[:WATCH_ROTATION]

    fetches = sent = 0
    for mal_id in queue:
        if fetches >= MAX_FETCHES or sent >= MAX_NOTIFS:
            break
        fetches += 1
        time.sleep(1.4)
        classe, det = fetch_entry(mal_id)
        if classe == "approved":
            if announce(det):
                notified.add(mal_id)
                watch.pop(mal_id, None)
                sent += 1
        elif classe == "error":
            print(f"  #{mal_id} : lecture impossible ({det.get('error', '?')})")
            info = watch.setdefault(mal_id, {"since": now.isoformat(), "tries": 0})
            info["since"] = now.isoformat()      # repasse en fin de rotation
        else:
            info = watch.setdefault(mal_id, {"since": now.isoformat(), "tries": 0})
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

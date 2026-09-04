#!/usr/bin/env python3
"""mal-notify — prévient Discord quand MAL accepte une nouvelle entrée anime.

  1. on lit la page « nouvelles entrées » de MAL (les ~60 plus récentes) ;
  2. tout ID jamais vu est vérifié via Jikan (miroir public de la base MAL) ;
  3. si l'entrée n'est pas encore validée, elle passe en file d'attente et sera
     re-vérifiée à chaque run — la notif part à la VALIDATION, pas à l'ID ;
  4. on compare des listes d'IDs, jamais « l'ID le plus grand », donc un ancien
     ID accepté hors ordre est détecté comme une entrée neuve.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------- réglages
MAL_PAGE = "https://myanimelist.net/anime.php?o=9&c%5B0%5D=a&c%5B1%5D=d&cv=2&w=1"
JIKAN    = "https://api.jikan.moe/v4"
WEBHOOK  = os.environ["WEBHOOK_URL"]      # fourni par le secret GitHub

STATE_FILE     = Path("state.json")
SHOW_OFFSETS   = [0, 20, 40]              # ~60 dernières entrées de la page
MAX_NOTIFS     = 10                       # garde-fou anti-spam par run
WATCH_TTL_DAYS = 60                       # abandon d'une entrée jamais validée
ALERT_AFTER    = 20                       # alerte Discord après 20 échecs réseau

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- état
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"notified": [], "watch": {}, "failures": 0, "alerted": False}

def save_state(state):
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, indent=1))

# ---------------------------------------------------------------- réseau
def get_html(url):
    """GET sur MAL en contournant Cloudflare (3 méthodes en cascade)."""
    try:                                   # 1. curl_cffi : vraie empreinte Chrome
        from curl_cffi import requests as cffi
        r = cffi.get(url, impersonate="chrome", timeout=30)
        if r.status_code == 200 and "just a moment" not in r.text.lower():
            return r.text
    except Exception:
        pass
    try:                                   # 2. cloudscraper
        import cloudscraper
        r = cloudscraper.create_scraper().get(url, timeout=30)
        if r.status_code == 200 and "just a moment" not in r.text.lower():
            return r.text
    except Exception:
        pass
    r = requests.get(url, timeout=30, headers={"User-Agent": UA})  # 3. secours
    if r.status_code != 200 or "just a moment" in r.text.lower():
        raise RuntimeError(f"MAL illisible (HTTP {r.status_code})")
    return r.text

def scrape_window():
    """IDs visibles sur la page, du plus récent au plus ancien."""
    ids = []
    for show in SHOW_OFFSETS:
        url = MAL_PAGE if show == 0 else f"{MAL_PAGE}&show={show}"
        html = get_html(url)
        html = html.split('id="content"', 1)[-1]   # saute le menu, garde les résultats
        ids += re.findall(r'href="[^"]*/anime/(\d+)/', html)
        time.sleep(1.5)
    return list(dict.fromkeys(ids))                # dédoublonne, garde l'ordre

def jikan(mal_id):
    """Détails d'une entrée. None = inconnue ou pas encore accessible."""
    time.sleep(0.4)                                # Jikan : max 3 requêtes/s
    try:
        r = requests.get(f"{JIKAN}/anime/{mal_id}", timeout=20)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()["data"]
    except Exception:
        return None

# ---------------------------------------------------------------- discord
def send_embed(embed):
    for _ in range(3):
        r = requests.post(WEBHOOK, json={"embeds": [embed]}, timeout=15)
        if r.status_code in (200, 204):
            return
        time.sleep(float(r.json().get("retry_after", 2)) if r.status_code == 429 else 2)
    print(f"!! échec d'envoi Discord : {embed.get('title')}")

def announce(entry):
    mal_id = entry["mal_id"]
    embed = {
        "title": entry.get("title") or f"Nouvelle entrée #{mal_id}",
        "url":   f"https://myanimelist.net/anime/{mal_id}",
        "color": 0x2E51A2,
        "fields": [
            {"name": "Type",   "value": entry.get("type")   or "?", "inline": True},
            {"name": "Statut", "value": entry.get("status") or "?", "inline": True},
        ],
        "footer": {"text": f"MAL #{mal_id}"},
    }
    synopsis = (entry.get("synopsis") or "").strip()
    image    = entry.get("images", {}).get("jpg", {}).get("image_url", "")
    if synopsis:
        embed["description"] = synopsis[:300]
    if image:
        embed["thumbnail"] = {"url": image}
    send_embed(embed)
    print(f"  -> notifié : #{mal_id} — {embed['title']}")

# ---------------------------------------------------------------- main
def main():
    state = load_state()

    # 1) lire la page MAL -------------------------------------------------
    try:
        window = scrape_window()
        if len(window) < 10:
            raise RuntimeError(f"seulement {len(window)} entrées lues (parsing cassé ?)")
    except Exception as exc:
        state["failures"] += 1
        print(f"échec de lecture de MAL ({state['failures']} d'affilée) : {exc}")
        if state["failures"] >= ALERT_AFTER and not state.get("alerted"):
            send_embed({"title": "⚠️ mal-notify n'arrive plus à lire MAL",
                        "description": (f"{state['failures']} tentatives ratées d'affilée. "
                                        "Vérifie les logs dans l'onglet Actions du repo."),
                        "color": 0xE67E22})
            state["alerted"] = True
        save_state(state)
        return

    state["failures"] = state["alerted"] = 0
    print(f"page MAL lue : {len(window)} entrées dans la fenêtre")

    notified = set(state["notified"])
    watch    = state["watch"]        # IDs vus mais pas encore validés
    now      = datetime.now(timezone.utc)

    # 2) tout premier run : mémoriser l'existant sans rien envoyer ---------
    if not notified and not watch:
        print("premier lancement : initialisation silencieuse")
        for mal_id in window:
            entry = jikan(mal_id)
            if entry is None or entry.get("approved"):
                notified.add(mal_id)      # déjà validée -> considérée comme vue
            else:
                watch[mal_id] = {"since": now.isoformat(), "tries": 0}
        state["notified"] = sorted(notified, key=int)
        state["watch"] = watch
        save_state(state)
        print(f"initialisé : {len(notified)} validées, {len(watch)} en attente")
        return

    # 3) purger les entrées en attente depuis trop longtemps ----------------
    for mal_id, info in list(watch.items()):
        age = (now - datetime.fromisoformat(info["since"])).days
        if age > WATCH_TTL_DAYS:
            print(f"#{mal_id} jamais validée depuis {age} j -> abandon")
            del watch[mal_id]
            notified.add(mal_id)

    # 4) examiner : nouveaux IDs de la page + file d'attente ----------------
    fresh = [i for i in window if i not in notified and i not in watch]
    print(f"{len(fresh)} nouvel(s) ID, {len(watch)} en attente de validation")
    sent = 0
    for mal_id in list(reversed(fresh)) + list(watch):   # ancien -> récent
        if sent >= MAX_NOTIFS:
            break                    # anti-spam : le reste attendra le prochain run
        entry = jikan(mal_id)
        if entry and entry.get("approved"):
            announce(entry)
            notified.add(mal_id)
            watch.pop(mal_id, None)
            sent += 1
        else:
            # ID attribué mais pas encore validée -> on recheckera plus tard
            watch.setdefault(mal_id, {"since": now.isoformat(), "tries": 0})
            watch[mal_id]["tries"] += 1

    # 5) sauvegarder ---------------------------------------------------------
    state["notified"] = sorted(notified, key=int)[-2000:]
    state["watch"]    = watch
    save_state(state)
    print(f"terminé : {sent} notification(s) envoyée(s)")

if __name__ == "__main__":
    main()

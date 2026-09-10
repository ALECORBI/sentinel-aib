#!/usr/bin/env python3
"""
Sentinel AIB — Fase 1
Script di raccolta dati: scarica gli hotspot satellitari NASA FIRMS per la
Sardegna, li accumula in uno storico persistente (database SQLite) e applica
due filtri per ridurre i falsi allarmi, prima ancora che arrivino a un
Comune o a un operatore:

  1. FILTRO ALBA/TRAMONTO: la luce radente del sole basso può riflettersi
     su superfici (serre, pannelli fotovoltaici, specchi d'acqua) e generare
     falsi hotspot anche di giorno. Calcoliamo l'orario esatto di
     alba/tramonto per ogni punto e data (formula astronomica NOAA, nessun
     servizio esterno necessario) e marchiamo come "sospetto" ogni
     rilevamento troppo vicino a quella finestra.

  2. FILTRO FONTI FISSE RICORRENTI: se un punto si "accende" ripetutamente
     nello stesso posto in giorni diversi, è quasi certamente una fonte di
     calore fissa (industria, centrale, raffineria) e non un incendio.
     Lo storico stesso "impara" queste posizioni col tempo. In aggiunta,
     una piccola lista dei principali siti industriali noti in Sardegna
     dà una prima protezione anche prima che lo storico sia abbastanza lungo.

Nessuno di questi filtri CANCELLA i dati: tutto resta nel database, i punti
sospetti vengono solo marcati (is_twilight_suspect, is_recurring_source,
near_industrial_site) così la dashboard può mostrarli in modo diverso
invece di trattarli come un incendio vero.

USO:
    1. Richiedi una MAP_KEY gratuita (istantanea): https://firms.modaps.eosdis.nasa.gov/api/map_key/
    2. Esporta la chiave come variabile d'ambiente:
         export FIRMS_MAP_KEY="la-tua-chiave"
    3. Esegui:
         python3 fetch_firms.py

COSA PRODUCE:
    - data/sentinel_aib.db      -> database SQLite con tutto lo storico (fonte di verità)
    - data/history.json         -> esportazione dello storico con i flag di affidabilità,
                                    letta dalla dashboard (index.html) tramite "Carica storico"
    - data/history_summary.json -> conteggi aggregati per giorno (totali e sospetti)

USO CONTINUATIVO (consigliato):
    Pianifica questo script con cron ogni 3-6 ore (in linea con
    l'aggiornamento dei dati VIIRS), ad esempio:
        0 */3 * * *  cd /percorso/progetto && FIRMS_MAP_KEY=xxx python3 fetch_firms.py
    Ogni esecuzione aggiunge solo i rilevamenti nuovi (deduplicati), quindi
    è sicuro eseguirlo spesso senza duplicare i dati. Più lo storico cresce,
    più il filtro "fonti fisse ricorrenti" diventa preciso da solo.

LIMITE IMPORTANTE (da non nascondere a nessuno): questi filtri riducono i
falsi allarmi, ma i dati restano satellitari — un rilevamento arriva con
ore di ritardo rispetto all'innesco reale. Per il vero tempo reale serve il
rilevamento a terra (sensori IoT), previsto nella Fase 2 del piano.
"""

import csv
import io
import json
import math
import os
import sqlite3
import sys
import urllib.request
from datetime import date, datetime, timezone

# Bounding box Sardegna: west, south, east, north
SARDINIA_BBOX = "8.05,38.80,9.90,41.35"

# Fonte satellitare. Alternative utili:
#   MODIS_NRT             - MODIS, risoluzione ~1km, storico più lungo
#   VIIRS_SNPP_NRT         - VIIRS Suomi-NPP, risoluzione ~375m (più precisa)
#   VIIRS_NOAA20_NRT        - VIIRS NOAA-20, seconda copertura giornaliera
SOURCE = "VIIRS_SNPP_NRT"

DAY_RANGE = 2  # ultimi 2 giorni (margine di sicurezza se lo script salta un'esecuzione)

# --- Filtro 1: finestra di sospetto attorno ad alba/tramonto ---
TWILIGHT_WINDOW_MINUTES = 40  # quanto prima/dopo alba e tramonto considerare "sospetto"

# --- Filtro 2: fonti fisse ricorrenti ---
RECURRING_GRID_DECIMALS = 2      # ~1.1 km di lato: punti arrotondati a questa griglia
RECURRING_MIN_DISTINCT_DAYS = 3  # giorni diversi minimi per considerarla una fonte fissa

# --- Siti industriali noti in Sardegna (punto di partenza, lista non esaustiva) ---
# Coordinate approssimate del baricentro dell'area industriale/impianto.
KNOWN_INDUSTRIAL_SITES = [
    {"name": "Polo industriale Sarroch (raffineria Saras)", "lat": 39.0730, "lon": 9.0150, "radius_km": 3.0},
    {"name": "Polo industriale Portovesme / Portoscuso", "lat": 39.1940, "lon": 8.3930, "radius_km": 3.0},
    {"name": "Area industriale di Ottana", "lat": 40.2710, "lon": 9.0000, "radius_km": 2.5},
    {"name": "Polo petrolchimico di Porto Torres", "lat": 40.8460, "lon": 8.4040, "radius_km": 3.0},
    {"name": "Area industriale di Assemini/Macchiareddu", "lat": 39.2830, "lon": 9.0000, "radius_km": 3.0},
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "sentinel_aib.db")
HISTORY_JSON = os.path.join(DATA_DIR, "history.json")
SUMMARY_JSON = os.path.join(DATA_DIR, "history_summary.json")


def fetch(map_key: str) -> str:
    url = (
        f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
        f"{map_key}/{SOURCE}/{SARDINIA_BBOX}/{DAY_RANGE}"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# Filtro 1 — alba e tramonto (formula astronomica NOAA, nessuna libreria esterna)
# ---------------------------------------------------------------------------

def sunrise_sunset_utc_hours(lat: float, lon: float, on_date: date):
    """Restituisce (alba_utc_ore, tramonto_utc_ore) come numeri decimali (es. 5.75 = 05:45).
    Approssimazione standard NOAA, precisione tipica entro 1-2 minuti — più che
    sufficiente per individuare la finestra di luce radente."""
    n = on_date.timetuple().tm_yday
    lat_rad = math.radians(lat)
    gamma = 2 * math.pi / 365 * (n - 1)

    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma)
    )

    zenith = math.radians(90.833)  # angolo standard che include rifrazione atmosferica
    cos_h = (math.cos(zenith) / (math.cos(lat_rad) * math.cos(decl))
              - math.tan(lat_rad) * math.tan(decl))
    cos_h = max(-1.0, min(1.0, cos_h))  # sole sempre sopra/sotto l'orizzonte a lat estreme
    ha = math.degrees(math.acos(cos_h))

    solar_noon_min = 720 - 4 * lon - eqtime  # minuti da mezzanotte UTC
    sunrise_min = solar_noon_min - 4 * ha
    sunset_min = solar_noon_min + 4 * ha
    return sunrise_min / 60.0, sunset_min / 60.0


def is_twilight_suspect(lat: float, lon: float, acq_date: str, acq_time: str) -> bool:
    try:
        d = datetime.strptime(acq_date, "%Y-%m-%d").date()
        hh = int(acq_time.zfill(4)[:2])
        mm = int(acq_time.zfill(4)[2:4])
        t_hours = hh + mm / 60.0
    except (ValueError, IndexError):
        return False

    sunrise, sunset = sunrise_sunset_utc_hours(lat, lon, d)
    window = TWILIGHT_WINDOW_MINUTES / 60.0
    return (abs(t_hours - sunrise) <= window) or (abs(t_hours - sunset) <= window)


# ---------------------------------------------------------------------------
# Filtro 2 — vicinanza a siti industriali noti
# ---------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearby_industrial_site(lat: float, lon: float):
    for site in KNOWN_INDUSTRIAL_SITES:
        if haversine_km(lat, lon, site["lat"], site["lon"]) <= site["radius_km"]:
            return site["name"]
    return None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hotspots (
            id TEXT PRIMARY KEY,   -- lat|lon|data|ora|sorgente, per deduplicare
            latitude REAL,
            longitude REAL,
            acq_date TEXT,
            acq_time TEXT,
            confidence TEXT,
            frp REAL,                    -- fire radiative power (MW), intensità del segnale
            satellite TEXT,
            source TEXT,
            fetched_at TEXT,              -- quando lo script lo ha scaricato (non l'orario dell'evento)
            is_twilight_suspect INTEGER,  -- 1 = vicino ad alba/tramonto, possibile riflesso
            near_industrial_site TEXT     -- nome del sito industriale noto vicino, o NULL
        )
    """)
    conn.commit()


def row_id(row: dict) -> str:
    return "|".join([
        row.get("latitude", ""), row.get("longitude", ""),
        row.get("acq_date", ""), row.get("acq_time", ""), SOURCE,
    ])


def main():
    map_key = os.environ.get("FIRMS_MAP_KEY")
    if not map_key:
        print("Errore: variabile d'ambiente FIRMS_MAP_KEY non impostata.")
        print("Richiedi una chiave gratuita su https://firms.modaps.eosdis.nasa.gov/api/map_key/")
        sys.exit(1)

    print(f"Scarico hotspot {SOURCE} per la Sardegna (ultimi {DAY_RANGE} giorni)...")
    raw = fetch(map_key)

    if "Invalid" in raw:
        print("MAP_KEY non valida o richiesta malformata.")
        print(raw[:300])
        sys.exit(1)

    rows = list(csv.DictReader(io.StringIO(raw))) if raw.strip() else []

    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    fetched_at = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    new_count = 0
    for row in rows:
        rid = row_id(row)
        try:
            lat = float(row.get("latitude", 0) or 0)
            lon = float(row.get("longitude", 0) or 0)
            acq_date = row.get("acq_date", "")
            acq_time = row.get("acq_time", "")

            twilight = is_twilight_suspect(lat, lon, acq_date, acq_time)
            industrial = nearby_industrial_site(lat, lon)

            cur.execute(
                """INSERT OR IGNORE INTO hotspots
                   (id, latitude, longitude, acq_date, acq_time, confidence, frp, satellite,
                    source, fetched_at, is_twilight_suspect, near_industrial_site)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, lat, lon, acq_date, acq_time, row.get("confidence", ""),
                 float(row.get("frp", 0) or 0), row.get("satellite", ""), SOURCE, fetched_at,
                 1 if twilight else 0, industrial)
            )
            if cur.rowcount == 1:
                new_count += 1
        except (ValueError, TypeError):
            continue
    conn.commit()

    # --- Filtro 2b: fonti fisse ricorrenti, calcolato sull'intero storico ---
    # Raggruppa per griglia (lat/lon arrotondati) e conta i giorni distinti in cui
    # quel punto si è "acceso": se ricorre spesso, è una fonte fissa, non un incendio.
    recurring_cells = {}
    for lat, lon, acq_date in conn.execute("SELECT latitude, longitude, acq_date FROM hotspots"):
        key = (round(lat, RECURRING_GRID_DECIMALS), round(lon, RECURRING_GRID_DECIMALS))
        recurring_cells.setdefault(key, set()).add(acq_date)
    recurring_keys = {k for k, days in recurring_cells.items() if len(days) >= RECURRING_MIN_DISTINCT_DAYS}

    # Esporta l'intero storico in JSON (letto dalla dashboard), con tutti i flag
    all_rows = conn.execute(
        "SELECT latitude, longitude, acq_date, acq_time, confidence, frp, satellite, "
        "is_twilight_suspect, near_industrial_site FROM hotspots ORDER BY acq_date, acq_time"
    ).fetchall()

    history = []
    for r in all_rows:
        lat, lon = r[0], r[1]
        key = (round(lat, RECURRING_GRID_DECIMALS), round(lon, RECURRING_GRID_DECIMALS))
        is_recurring = key in recurring_keys
        history.append({
            "latitude": lat, "longitude": lon, "acq_date": r[2], "acq_time": r[3],
            "confidence": r[4], "frp": r[5], "satellite": r[6],
            "is_twilight_suspect": bool(r[7]),
            "near_industrial_site": r[8],
            "is_recurring_source": is_recurring,
            "recurring_days_count": len(recurring_cells.get(key, [])),
        })

    with open(HISTORY_JSON, "w", encoding="utf-8") as f:
        json.dump({"generated_at": fetched_at, "count": len(history), "hotspots": history}, f, ensure_ascii=False)

    # Conteggi aggregati per giorno, distinguendo probabili reali da sospetti
    per_day_total, per_day_suspect = {}, {}
    for h in history:
        per_day_total[h["acq_date"]] = per_day_total.get(h["acq_date"], 0) + 1
        if h["is_twilight_suspect"] or h["is_recurring_source"] or h["near_industrial_site"]:
            per_day_suspect[h["acq_date"]] = per_day_suspect.get(h["acq_date"], 0) + 1
    with open(SUMMARY_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": fetched_at,
            "per_day_total": per_day_total,
            "per_day_suspect": per_day_suspect,
        }, f, ensure_ascii=False, indent=2)

    suspect_count = sum(1 for h in history
                         if h["is_twilight_suspect"] or h["is_recurring_source"] or h["near_industrial_site"])
    print(f"Fatto. {len(rows)} righe ricevute da FIRMS, {new_count} nuove salvate nello storico.")
    print(f"Storico totale: {len(history)} rilevamenti, di cui {suspect_count} marcati come sospetti "
          f"(alba/tramonto, fonte ricorrente o sito industriale noto).")
    print(f"Esportato: {HISTORY_JSON} e {SUMMARY_JSON}")
    conn.close()


if __name__ == "__main__":
    main()

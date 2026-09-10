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

Inoltre, ogni hotspot viene associato al Comune sardo di appartenenza
(point-in-polygon sui confini ISTAT in sardegna_comuni.geojson), e per ogni
rilevamento NUOVO e NON sospetto viene inviato un alert automatico via
Telegram e/o email (entrambi opzionali e indipendenti, vedi README per la
configurazione dei secrets TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID e
SMTP_USER/SMTP_PASS/ALERT_EMAIL_TO).

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
import smtplib
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from email.mime.text import MIMEText

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
COMUNI_GEOJSON = os.path.join(BASE_DIR, "sardegna_comuni.geojson")


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
# Comune di appartenenza (point-in-polygon sui confini comunali ISTAT)
# ---------------------------------------------------------------------------
# Caricato una sola volta e riusato per tutti i punti dell'esecuzione. Se lo
# script gira senza il file sardegna_comuni.geojson accanto (o senza shapely
# installato) il campo "comune" resta semplicemente vuoto: non blocca il resto.

_COMUNI_SHAPES = None  # lista di (nome_comune, shapely_geometry), calcolata pigra


def _load_comuni_shapes():
    global _COMUNI_SHAPES
    if _COMUNI_SHAPES is not None:
        return _COMUNI_SHAPES
    _COMUNI_SHAPES = []
    try:
        from shapely.geometry import shape
        if not os.path.exists(COMUNI_GEOJSON):
            print(f"Attenzione: {COMUNI_GEOJSON} non trovato, il campo 'comune' resterà vuoto.")
            return _COMUNI_SHAPES
        with open(COMUNI_GEOJSON, encoding="utf-8") as f:
            data = json.load(f)
        for feat in data.get("features", []):
            name = feat.get("properties", {}).get("comune")
            geom = shape(feat["geometry"])
            _COMUNI_SHAPES.append((name, geom))
    except ImportError:
        print("Attenzione: libreria 'shapely' non disponibile, il campo 'comune' resterà vuoto.")
    return _COMUNI_SHAPES


def find_comune(lat: float, lon: float, max_distance_km: float = 5.0):
    """Trova il Comune sardo a cui appartiene il punto. Se il punto cade fuori
    da tutti i confini (es. leggermente in mare, per l'incertezza di
    localizzazione del satellite), restituisce il Comune più vicino entro
    max_distance_km, preceduto da '~' per indicare che è un'approssimazione.
    Restituisce None se non c'è nessun Comune abbastanza vicino."""
    shapes = _load_comuni_shapes()
    if not shapes:
        return None
    try:
        from shapely.geometry import Point
    except ImportError:
        return None

    point = Point(lon, lat)
    for name, geom in shapes:
        if geom.contains(point):
            return name

    # Nessun contenimento diretto: cerca il confine più vicino (in gradi,
    # poi convertito approssimativamente in km per il confronto con la soglia)
    best_name, best_deg = None, None
    for name, geom in shapes:
        d = geom.distance(point)
        if best_deg is None or d < best_deg:
            best_deg, best_name = d, name
    if best_name is None:
        return None
    # 1 grado ~= 111 km alla latitudine sarda; approssimazione sufficiente
    # per decidere se il Comune più vicino è "abbastanza vicino"
    approx_km = best_deg * 111.0
    if approx_km <= max_distance_km:
        return f"~{best_name}"
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
            near_industrial_site TEXT,    -- nome del sito industriale noto vicino, o NULL
            comune TEXT,                  -- Comune sardo di appartenenza (o "~Nome" se approssimato), o NULL
            alerted INTEGER DEFAULT 0     -- 1 = alert già inviato per questo hotspot (Telegram/email)
        )
    """)
    conn.commit()
    # Migrazione morbida: se il DB esiste già da prima di queste colonne, aggiungile.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(hotspots)")}
    if "comune" not in existing_cols:
        conn.execute("ALTER TABLE hotspots ADD COLUMN comune TEXT")
    if "alerted" not in existing_cols:
        conn.execute("ALTER TABLE hotspots ADD COLUMN alerted INTEGER DEFAULT 0")
    conn.commit()


def row_id(row: dict) -> str:
    return "|".join([
        row.get("latitude", ""), row.get("longitude", ""),
        row.get("acq_date", ""), row.get("acq_time", ""), SOURCE,
    ])


# ---------------------------------------------------------------------------
# Alert automatici (Telegram + email)
# ---------------------------------------------------------------------------
# Entrambi i canali sono opzionali e indipendenti: se le variabili d'ambiente
# relative a un canale non sono impostate (perché il Comune/utente non ha
# ancora configurato quel secret su GitHub), quel canale viene semplicemente
# saltato con un messaggio informativo — lo script non si blocca mai per
# questo. Per non spammare notifiche separate una per una, tutti gli hotspot
# "nuovi e non sospetti" trovati in una singola esecuzione vengono raccolti
# in UN solo messaggio/email per esecuzione (ogni 3 ore).

def _format_alert_text(items: list) -> str:
    lines = [f"🔥 Sentinel AIB — {len(items)} nuovo/i hotspot rilevato/i in Sardegna (non filtrato/i come falso allarme):", ""]
    for h in items:
        comune = h.get("comune") or "comune non determinato"
        lines.append(
            f"- {h['acq_date']} {h['acq_time']} UTC · {comune} · "
            f"coord. {h['latitude']:.3f},{h['longitude']:.3f} · "
            f"confidenza {h.get('confidence') or 'n/d'} · FRP {h.get('frp', 'n/d')} MW"
        )
    lines.append("")
    lines.append("Dati satellitari NASA FIRMS — verifica sempre prima di allertare risorse operative.")
    return "\n".join(lines)


def send_telegram_alert(items: list):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Alert Telegram non inviato: TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID non configurati (opzionale).")
        return
    text = _format_alert_text(items)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        print(f"Alert Telegram inviato ({len(items)} hotspot).")
    except urllib.error.URLError as e:
        print(f"Errore nell'invio dell'alert Telegram: {e}")


def send_email_alert(items: list):
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("ALERT_EMAIL_TO")
    if not smtp_user or not smtp_pass or not to_addr:
        print("Alert email non inviato: SMTP_USER, SMTP_PASS o ALERT_EMAIL_TO non configurati (opzionale).")
        return
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    text = _format_alert_text(items)
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = f"[Sentinel AIB] {len(items)} nuovo/i hotspot rilevato/i in Sardegna"
    msg["From"] = smtp_user
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=20) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [to_addr], msg.as_string())
        print(f"Alert email inviato a {to_addr} ({len(items)} hotspot).")
    except (smtplib.SMTPException, OSError) as e:
        print(f"Errore nell'invio dell'alert email: {e}")


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
    new_row_ids = []  # id dei rilevamenti effettivamente nuovi in questa esecuzione (per gli alert)
    for row in rows:
        rid = row_id(row)
        try:
            lat = float(row.get("latitude", 0) or 0)
            lon = float(row.get("longitude", 0) or 0)
            acq_date = row.get("acq_date", "")
            acq_time = row.get("acq_time", "")

            twilight = is_twilight_suspect(lat, lon, acq_date, acq_time)
            industrial = nearby_industrial_site(lat, lon)
            comune = find_comune(lat, lon)

            cur.execute(
                """INSERT OR IGNORE INTO hotspots
                   (id, latitude, longitude, acq_date, acq_time, confidence, frp, satellite,
                    source, fetched_at, is_twilight_suspect, near_industrial_site, comune)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, lat, lon, acq_date, acq_time, row.get("confidence", ""),
                 float(row.get("frp", 0) or 0), row.get("satellite", ""), SOURCE, fetched_at,
                 1 if twilight else 0, industrial, comune)
            )
            if cur.rowcount == 1:
                new_count += 1
                new_row_ids.append(rid)
        except (ValueError, TypeError):
            continue
    conn.commit()

    # Backfill: righe salvate prima che esistesse il campo "comune" (o create
    # senza sardegna_comuni.geojson disponibile) vengono completate ora.
    missing_comune = conn.execute(
        "SELECT id, latitude, longitude FROM hotspots WHERE comune IS NULL"
    ).fetchall()
    if missing_comune:
        for rid, lat, lon in missing_comune:
            conn.execute("UPDATE hotspots SET comune=? WHERE id=?", (find_comune(lat, lon), rid))
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
        "SELECT id, latitude, longitude, acq_date, acq_time, confidence, frp, satellite, "
        "is_twilight_suspect, near_industrial_site, comune FROM hotspots ORDER BY acq_date, acq_time"
    ).fetchall()

    history = []
    alert_candidates = []       # nuovi in questa esecuzione E non sospetti: quelli da notificare
    alert_candidate_ids = []    # id corrispondenti, per marcare "alerted" solo su questi
    for r in all_rows:
        rid, lat, lon = r[0], r[1], r[2]
        key = (round(lat, RECURRING_GRID_DECIMALS), round(lon, RECURRING_GRID_DECIMALS))
        is_recurring = key in recurring_keys
        is_suspect = bool(r[8]) or is_recurring or bool(r[9])
        entry = {
            "latitude": lat, "longitude": lon, "acq_date": r[3], "acq_time": r[4],
            "confidence": r[5], "frp": r[6], "satellite": r[7],
            "is_twilight_suspect": bool(r[8]),
            "near_industrial_site": r[9],
            "is_recurring_source": is_recurring,
            "recurring_days_count": len(recurring_cells.get(key, [])),
            "comune": r[10],
        }
        history.append(entry)
        if rid in new_row_ids and not is_suspect:
            alert_candidates.append(entry)
            alert_candidate_ids.append(rid)

    with open(HISTORY_JSON, "w", encoding="utf-8") as f:
        json.dump({"generated_at": fetched_at, "count": len(history), "hotspots": history}, f, ensure_ascii=False)

    # --- Alert automatici: solo per i rilevamenti nuovi in questa esecuzione
    # e non marcati come sospetti da nessuno dei filtri. Un solo messaggio/email
    # per esecuzione (ogni 3 ore), anche se ci sono più hotspot nuovi insieme.
    if alert_candidates:
        print(f"{len(alert_candidates)} nuovo/i hotspot non sospetto/i: invio alert...")
        send_telegram_alert(alert_candidates)
        send_email_alert(alert_candidates)
        conn.executemany("UPDATE hotspots SET alerted=1 WHERE id=?", [(rid,) for rid in alert_candidate_ids])
        conn.commit()
    else:
        print("Nessun nuovo hotspot non sospetto in questa esecuzione: nessun alert da inviare.")

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

# Sentinel AIB — Prototipo Fase 1

Primo mattone concreto del piano: una dashboard che mostra su mappa gli
hotspot satellitari (fuochi/anomalie termiche) rilevati in Sardegna,
usando **dati satellitari pubblici e gratuiti** (NASA FIRMS). Nessun
hardware, nessun costo di infrastruttura — solo software.

Include due filtri per ridurre i falsi allarmi (spiegati sotto): uno per
i riflessi di luce radente ad alba/tramonto, uno per le fonti di calore
fisse (industrie, raffinerie) che altrimenti verrebbero scambiate per
incendi.

## Cosa c'è in questa cartella

| File | Cosa fa |
|---|---|
| `index.html` | Dashboard web (mappa Leaflet) da aprire nel browser. Interroga in tempo reale l'API NASA FIRMS. |
| `fetch_firms.py` | Script Python da riga di comando: scarica gli hotspot e li accumula in un database storico (SQLite) + esporta `history.json`. |
| `sardegna_boundary.geojson` / `sardegna_geo.js` | Confine geografico reale della Regione Sardegna (fonte: ISTAT, via openpolis/geojson-italy), usato dalla dashboard. |
| `data/history.json` | Esempio di storico già pronto (dati reali del 9-10 settembre 2026), da caricare subito nella dashboard per provare la funzione. |
| `.github/workflows/update-data.yml` | Automazione (GitHub Actions): esegue `fetch_firms.py` ogni 3 ore e aggiorna il sito da solo, una volta fatto il deploy (vedi sotto). |

## Come provarlo subito

1. Apri `index.html` con doppio click (si apre nel browser). La MAP_KEY è
   già precompilata e la mappa si carica da sola.
2. Se in Sardegna non ci sono incendi attivi in quel momento, la mappa
   resterà vuota — è normale, significa che il sistema funziona ma non
   c'è nulla da segnalare.
3. Se vuoi usare una tua chiave personale: richiedine una gratuita su
   https://firms.modaps.eosdis.nasa.gov/api/map_key/ e incollala nel
   campo dedicato — resta salvata nel browser (localStorage) per i
   prossimi accessi.

## Come costruire e vedere lo storico

```bash
export FIRMS_MAP_KEY="la-tua-chiave"
python3 fetch_firms.py
```

Ogni esecuzione scarica i rilevamenti recenti e li **accumula** (senza
duplicati) in `data/sentinel_aib.db` (database SQLite), oltre a esportare
`data/history.json` e `data/history_summary.json` (conteggi per giorno).

Per vederlo nella dashboard: apri `index.html`, nella sezione "Storico
rilevamenti" clicca su "Scegli file" e seleziona `data/history.json`. I
punti storici compaiono in grigio sulla mappa, distinti da quelli recenti
(arancioni). **Questo zip include già un `data/history.json` di esempio**
con dati veri scaricati il 9-10 settembre 2026, così puoi provare subito
la funzione senza dover prima lanciare lo script.

Pianificato con `cron` ogni 3-6 ore, lo script costruisce da solo uno
storico continuo nel tempo — utile sia per il prodotto (analisi del
rischio, trend) sia per le candidature ai bandi pubblici, che spesso
chiedono dati a supporto della proposta.

## Filtri anti-falso-allarme

Ogni hotspot, sia nello storico sia nella mappa in tempo reale, viene
controllato con due filtri (implementati identici sia in Python che in
JavaScript, così danno lo stesso risultato ovunque):

1. **Alba/tramonto**: la luce radente del sole basso può riflettersi su
   superfici (serre, pannelli fotovoltaici, specchi d'acqua) e generare
   falsi hotspot. Calcoliamo l'orario esatto di alba e tramonto per ogni
   punto e data (formula astronomica NOAA, nessun servizio esterno) e
   marchiamo come sospetto ogni rilevamento entro 40 minuti da quella
   finestra.
2. **Fonti fisse ricorrenti**: se un punto si "accende" ripetutamente
   nello stesso posto in almeno 3 giorni diversi, lo storico stesso lo
   riconosce come una fonte di calore fissa (industria, centrale) e non
   un incendio — nessuna lista da mantenere a mano, il sistema impara da
   solo più lo storico cresce. In aggiunta c'è una prima lista manuale dei
   maggiori siti industriali sardi noti (raffineria di Sarroch,
   Portovesme, Ottana, Porto Torres, Assemini), utile finché lo storico
   non è abbastanza lungo da riconoscerli da solo.

I punti sospetti **non vengono cancellati**, solo marcati e mostrati in
grigio sulla mappa (invece che arancione/rosso), con il motivo indicato
nel popup. Nello storico di esempio incluso in questo zip, uno degli 8
rilevamenti risulta infatti vicino alla raffineria di Sarroch — quasi
certamente un pennacchio di calore dell'impianto, non un incendio: un
esempio reale di come il filtro dovrebbe funzionare.

**Limite importante**: questi filtri riducono i falsi allarmi, ma restano
dati satellitari con ore di ritardo rispetto all'evento reale. Per il
vero tempo reale serve il rilevamento a terra della Fase 2 — nessun
filtro software cambia questo, è un limite fisico dei satelliti che
passano sopra la Sardegna solo un paio di volte al giorno.

## Deploy online (link pubblico, gratuito, sempre aggiornato)

Con GitHub Pages ottieni un link pubblico stabile (tipo
`https://tuonome.github.io/sentinel-aib-fase1/`) da mandare a un Comune o
mostrare a un investitore, senza spiegare come aprire un file. In più, un
piccolo robot (GitHub Actions, incluso e gratuito) esegue `fetch_firms.py`
da solo ogni 3 ore e aggiorna lo storico sul sito — non devi più lanciarlo
a mano dal tuo computer.

**Passi (10 minuti, una volta sola):**

1. Crea un account gratuito su [github.com](https://github.com/) se non
   ce l'hai già.
2. Crea un nuovo repository (pulsante verde "New"), pubblico, chiamalo
   ad esempio `sentinel-aib`.
3. Carica dentro tutto il contenuto di questa cartella (su GitHub, nella
   pagina del repository appena creato: "uploading an existing file",
   trascina tutti i file e le cartelle — incluse `.github` e `data`).
4. Vai su **Settings → Secrets and variables → Actions → New repository
   secret**. Nome: `FIRMS_MAP_KEY`. Valore: la tua chiave NASA FIRMS
   (quella già nel file `index.html`, oppure richiedine una nuova su
   https://firms.modaps.eosdis.nasa.gov/api/map_key/). Questo permette
   al robot di scaricare i dati senza che la chiave sia scritta in chiaro
   nel codice pubblico.
5. Vai su **Settings → Pages**, in "Source" scegli il branch `main` e
   cartella `/ (root)`, salva. Dopo un paio di minuti il sito è online
   all'indirizzo che GitHub mostra in quella pagina.
6. (Opzionale ma consigliato) Vai su **Actions**, apri il workflow
   "Aggiorna storico incendi (FIRMS)" e clicca "Run workflow" per farlo
   partire subito la prima volta, invece di aspettare le 3 ore.

Da quel momento il sito resta online da solo, con lo storico che si
aggiorna automaticamente — nessun computer acceso, nessun costo.

## Limiti di questo prototipo (onesti, non nasconderli a chi lo guarda)

- Il confine mostrato è quello **regionale** (Sardegna nel suo insieme),
  non ancora suddiviso per singolo Comune — utile per farsi un'idea, non
  ancora per operare Comune per Comune.
- Senza deploy online, lo storico va caricato manualmente (selezionando
  il file) perché un file HTML aperto in locale non può leggere altri
  file da solo per motivi di sicurezza del browser. Con il deploy (sopra)
  si carica automaticamente.
- Non ci sono ancora alert automatici (email/Telegram): oggi bisogna
  aprire la pagina per vedere i dati.
- FIRMS ha una risoluzione di localizzazione di alcune centinaia di metri
  e un ritardo di qualche ora rispetto all'evento reale — è un ottimo
  layer di partenza, non sostituisce il rilevamento a terra della Fase 2.
- Alcuni hotspot hanno "confidenza bassa" (`n` nei dati): possono essere
  falsi positivi (superfici calde riflettenti, altre fonti di calore), non
  sempre incendi veri.

## Prossimi passi tecnici

1. Aggiungere i confini dei singoli Comuni sardi (stesso procedimento
   usato per il confine regionale, dati ISTAT open data).
2. Aggiungere alert automatici (email o Telegram bot, più semplice di
   SMS per partire) quando un hotspot non sospetto cade vicino a un'area
   definita "sensibile" (es. buffer attorno a un centro abitato) — con il
   deploy online già pronto, il robot che aggiorna i dati può anche
   mandare la notifica nello stesso passaggio.
3. Valutare l'aggiunta di EFFIS (Copernicus) come seconda fonte
   satellitare — richiede di verificare gli endpoint aggiornati con il
   team EFFIS (jrc-effis@ec.europa.eu), non ancora integrato qui.
4. Ampliare la lista dei siti industriali noti e/o abbassare la soglia
   dei "giorni diversi" per il filtro delle fonti ricorrenti, se in
   pratica emergono altri falsi positivi frequenti.

## Perché partire da qui

Questo prototipo dimostra, con dati veri, che il rilevamento satellitare
di base è ottenibile in giorni e a costo quasi zero. È il pezzo giusto da
mostrare a un Comune o in una candidatura a un bando: prova concreta che
il rischio tecnico della Fase 1 è basso, prima ancora di investire in
sensori a terra (Fase 2).

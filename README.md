# polymarket-watch

Watcher de wallets de Polymarket. Cada 5 minutos revisa 10 wallets (5 activas + 5 en vigilancia) y manda alertas a Telegram cuando una wallet activa hace un trade que pasa sus filtros (side, categoría, umbral USDC).

- Hosting: GitHub Actions (free tier ilimitado en repos públicos)
- Data source: `data-api.polymarket.com` (free, sin API key)
- Notificación: Telegram bot
- Costo total: $0

## Cómo funciona

```
GitHub Actions cron (cada 5 min)
    ↓
polymarket_watch.py
    ↓
data-api.polymarket.com  (lista de trades por wallet)
    ↓
Compara contra state/state.json (qué tx ya alertamos)
    ↓
Filtra cada trade nuevo por reglas per-wallet:
  - enabled (true/false)
  - alert_side (BUY/SELL)
  - alert_categories (lista de categorías permitidas)
  - alert_min_size_usdc (umbral USDC)
    ↓
Si pasa los filtros: manda mensaje a Telegram
    ↓
Commitea state/ actualizado al repo (push con retry+rebase)
```

## Setup en GitHub

### 1. Sube el folder a un repo

```bash
cd "Dashboard_Polymarket/automation"
git init -b main
git add .
git commit -m "initial: polymarket-watch"
git remote add origin https://github.com/<TU_USER>/polymarket-watch.git
git push -u origin main
```

> Para que el cron de cada 5 min sea gratis, el repo debe ser **público**.
> Repo privado funciona pero está limitado a 2,000 min/mes de Actions.
> Los secrets quedan privados aunque el repo sea público.

### 2. Configura los Secrets

En GitHub: **Settings → Secrets and variables → Actions → New repository secret**

| Nombre | Valor | Obligatorio |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | token de @BotFather | ✅ sí |
| `TELEGRAM_CHAT_ID` | chat_id de @userinfobot | ✅ sí |
| `GMAIL_USER` | tu Gmail (ej. `oscar@gmail.com`) | opcional |
| `GMAIL_APP_PASSWORD` | app password de 16 chars | opcional |
| `GMAIL_TO` | a quién mandar email (default: `GMAIL_USER`) | opcional |

Variables (overrides globales en `Settings → Variables`, opcionales):

| Nombre | Default | Comentario |
|---|---|---|
| `ALERT_MIN_SIZE_USDC` | `50000` | fallback si una wallet no define el suyo |
| `ALERT_NEW_MARKET_MIN_USDC` | `10000` | idem para nuevos mercados |

Casi todas las wallets definen su propio threshold en `tracked_wallets.json`,
así que estos defaults globales rara vez se usan.

### 3. Test manual

GitHub → **Actions** → **polymarket-watch** → **Run workflow**.

- Primera corrida: bootstrap state, no manda alertas, te llega 1 mensaje "🤖 polymarket-watch online".
- Corridas siguientes: detecta trades nuevos y alerta según las reglas per-wallet.

## Modificar config

Edita `tracked_wallets.json`, commit, push. La siguiente corrida (≤5 min) ya usa la nueva config.

```json
{
  "address": "0x...",
  "nick": "Mi alias",
  "enabled": true,
  "alert_side": "BUY",
  "alert_categories": ["NBA","NFL","Soccer"],
  "alert_min_size_usdc": 5000,
  "alert_new_market_min_usdc": 1000,
  "note": "comentario libre"
}
```

Categorías válidas (ver `polymarket_watch.py CATEGORY_KEYWORDS`):

```
Deportes:    NBA, NFL, MLB, NHL, Soccer, Golf, Tennis, UFC/MMA, Boxing,
             Cricket/Racing/Other Sports
Política:    Politics, Geopolitics, Macro/Fed
Otros:       Crypto, Tech/AI, Entertainment, Weather/Disasters, Other
```

Si una categoría no aparece, el trade cae en `Other`. Para no perder señales,
agrega `"Other"` a `alert_categories` de la wallet.

### Disable temporalmente una wallet

Cambia `"enabled": true` → `"enabled": false`. El state se preserva (cuando la
reactivas, sigue desde donde se quedó, sin re-alertar trades viejos).

### Cambiar frecuencia del cron

Edita `.github/workflows/watch.yml`, línea `cron`:
- Cada 5 min (default): `"*/5 * * * *"`
- Cada 15 min: `"*/15 * * * *"`
- Cada hora: `"0 * * * *"`

## Categorización del título del mercado

`polymarket_watch.py:categorize_market()` clasifica cada trade por keywords del
título. Cobertura actual: **99% de los trades de las 5 wallets activas**.

Si ves muchos trades cayendo en `Other`, expande `CATEGORY_KEYWORDS` con las
keywords nuevas y haz commit.

## Ver logs

- **Cada corrida:** GitHub → Actions → polymarket-watch → la corrida → expandir "Run watcher"
- **Histórico de alertas:** archivo `state/alerts.log` se commitea con cada run que envió alertas
- **State actual:** `state/state.json` (tx_hashes vistos, mercados conocidos, last_run)

## Limitaciones honestas

- **Delay máximo ≈ 5 min** (cron) + **30-60s** (latencia ingestion API). Total ~6 min máximo.
- **No es tiempo real.** Polymarket no tiene webhooks públicos. La única forma de "real time" sería WebSocket directo a su CLOB, fuera del scope.
- **`size` en la API es shares**, no USDC. El USDC notional se calcula `size × price`. Está documentado en el código.
- **Rate limit Polymarket:** no documentado oficialmente. Con 10 wallets cada 5 min (12/hora) no llegamos a límites observables.
- **Limit=200 trades por wallet por fetch.** Wallets con >200 fills entre runs (poco común) podrían perder los más viejos. Ninguna wallet trackeada llega a este volumen.
- **Categorización por keywords**: cubre 99% pero NO 100%. Mercados nuevos o con keywords no incluidas caen en `Other`. Solución: expandir el dict en `polymarket_watch.py`.
- **Race conditions de push:** cuando 2 runs corren cerca, uno puede fallar al pushear state. El workflow tiene retry+rebase con jitter (5 intentos). Si los 5 fallan el run termina con error y la siguiente corrida lo recupera.
- **State del bot vs trigger manual:** si haces `gh workflow run` manualmente seguido, los runs colisionan. El concurrency group del workflow evita 2 runs en paralelo, pero hace cola.

## Estructura

```
.
├── .github/workflows/watch.yml    ← cron cada 5 min + push con retry
├── polymarket_watch.py             ← script principal
├── tracked_wallets.json            ← config de wallets (10)
├── requirements.txt                ← solo `requests`
├── README.md                       ← este archivo
├── .gitignore
└── state/
    ├── .gitkeep
    ├── state.json                  ← snapshot (creado/actualizado por el bot)
    └── alerts.log                  ← histórico de alertas enviadas (TG_OK / TG_FAIL)
```

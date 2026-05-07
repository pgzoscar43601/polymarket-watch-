# polymarket-watch

Watcher de wallets de Polymarket. Cada hora revisa 9 wallets, detecta trades nuevos y manda alertas a Telegram.

- Free hosting: GitHub Actions (~360 min/mes de un free tier de 2,000)
- Free data source: `data-api.polymarket.com` (sin API key)
- Free notification: Telegram bot

## Cómo funciona

```
Cron de GitHub Actions cada hora
    ↓
polymarket_watch.py
    ↓
data-api.polymarket.com  (lista de trades por wallet)
    ↓
Compara contra state/state.json (última corrida)
    ↓
Si hay un trade nuevo > $50k USDC:
    → manda mensaje a Telegram
    → si Gmail está configurado, manda email también
    ↓
Commitea state/state.json actualizado al repo
```

## Setup en GitHub

### 1. Sube este folder a un repo nuevo en GitHub

```bash
cd "Dashboard_Polymarket/automation"
git init
git add .
git commit -m "initial: polymarket-watch"
git branch -M main
git remote add origin https://github.com/<TU_USER>/polymarket-watch.git
git push -u origin main
```

(El repo puede ser **privado** sin problema. GitHub Actions corre igual.)

### 2. Configura los Secrets del repo

En GitHub: **Settings → Secrets and variables → Actions → New repository secret**

| Nombre | Valor | Obligatorio |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | el token de @BotFather | ✅ sí |
| `TELEGRAM_CHAT_ID`   | tu chat_id (de @userinfobot) | ✅ sí |
| `GMAIL_USER`         | tu Gmail (ej. `oscar@gmail.com`) | opcional |
| `GMAIL_APP_PASSWORD` | app password de 16 chars de Gmail | opcional |
| `GMAIL_TO`           | a quién mandar (default: `GMAIL_USER`) | opcional |

Variables (en lugar de secrets, pueden ir en **Settings → Variables**):

| Nombre | Default | Significa |
|---|---|---|
| `ALERT_MIN_SIZE_USDC` | `50000` | trade mínimo (USDC) para alertar |
| `ALERT_NEW_MARKET_MIN_USDC` | `10000` | trade mínimo en mercado nuevo |

### 3. Test manual

En GitHub → pestaña **Actions** → **polymarket-watch** → botón **Run workflow** → Run.

- **Primera corrida**: NO manda alertas (bootstrap state). Te llega un mensaje de bienvenida del bot.
- **A partir de la segunda**: detecta trades nuevos y alerta.

## Modificar wallets trackeadas

Edita `tracked_wallets.json`, commit, push. La siguiente corrida empieza a trackearlos.

```json
[
  {"address": "0x...", "nick": "Mi alias", "note": "opcional"}
]
```

## Cambiar frecuencia

Edita `.github/workflows/watch.yml`, línea `cron: "0 * * * *"`:
- Cada hora (default): `"0 * * * *"`
- Cada 30 min: `"*/30 * * * *"`
- Cada 2 horas: `"0 */2 * * *"`
- Cada 4 horas: `"0 */4 * * *"`

## Ver logs

- Cada corrida: GitHub → Actions → polymarket-watch → la corrida que quieras → expandir "Run watcher".
- Histórico de alertas: el archivo `state/alerts.log` se commitea con cada corrida.

## Costo

- GitHub Actions: free tier 2,000 min/mes para repos privados, ilimitado para públicos
  - Esta workflow tarda ~30 seg → ~360 min/mes con cron horario → bien dentro del free
- Polymarket Data API: free, sin key, sin límite documentado
- Telegram bot: free
- **Total: $0**

## Limitaciones honestas

- Si Polymarket cambia el formato de su data API, el script puede romperse hasta que se actualice.
- Solo cubre trades a través del mainnet Polygon. NegRisk + CTF están cubiertos por la data API.
- `size` en la API es la cantidad de shares; el USDC notional se calcula `size * price`.
- State es por-wallet, snapshot del último run; trades muy recientes (último minuto) podrían perderse si la API tarda en indexarlos. Acceptable para alertas hourly.
- Si una wallet hace > 200 trades en una hora, los más viejos se cortan (limit=200). Los wallets que trackeamos no son tan activos, así que es OK.

## Estructura

```
.
├── .github/workflows/watch.yml   ← cron horario
├── polymarket_watch.py            ← script principal
├── tracked_wallets.json           ← config de wallets
├── requirements.txt               ← solo `requests`
├── README.md                      ← este archivo
└── state/
    ├── .gitkeep
    ├── state.json                 ← snapshot (creado/actualizado por el script)
    └── alerts.log                 ← histórico de alertas
```

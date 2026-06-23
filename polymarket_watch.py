#!/usr/bin/env python3
"""Polymarket wallet watcher.

Corre cada 5 min via GitHub Actions cron. Para cada wallet en
tracked_wallets.json revisa trades nuevos vía Polymarket Data API (free, sin
key) y manda alertas a Telegram según las reglas per-wallet (side, categoría,
umbral USDC).

State persistente en state/state.json (commiteado de vuelta al repo cada run
para que los siguientes runs sepan qué tx_hashes ya alertaron).

Env vars (GitHub Secrets/Variables):
  TELEGRAM_BOT_TOKEN          - obligatorio (secret)
  TELEGRAM_CHAT_ID            - obligatorio (secret)
  ALERT_MIN_SIZE_USDC         - opcional fallback global (var), default 50000
                                — la mayoría de wallets define el suyo en JSON
  ALERT_NEW_MARKET_MIN_USDC   - opcional fallback global (var), default 10000
  GMAIL_USER                  - opcional (secret) — fallback email backup
  GMAIL_APP_PASSWORD          - opcional (secret)
  GMAIL_TO                    - opcional (secret), default = GMAIL_USER

Per-wallet config (tracked_wallets.json):
  enabled                     - bool, default true
  alert_side                  - "BUY"/"SELL", default ambos
  alert_categories            - lista, default todas
  alert_min_size_usdc         - float, default ALERT_MIN_SIZE_USDC (umbral por-fill; subir alto = solo whale)
  alert_new_market_min_usdc   - float, default ALERT_NEW_MARKET_MIN_USDC
  alert_position_usd          - float, floor de POSICIÓN ACUMULADA. Alerta cuando la
                                posición del wallet en un mercado cruza niveles del
                                POSITION_LADDER >= este floor (anti-saturación: 1 alerta
                                por cruce, no por cada fill chiquito). default ALERT_POSITION_USD.
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import requests

CDMX_TZ = ZoneInfo("America/Mexico_City")

REPO_ROOT = Path(__file__).resolve().parent
STATE_PATH = REPO_ROOT / "state" / "state.json"
WALLETS_PATH = REPO_ROOT / "tracked_wallets.json"
ALERTS_LOG = REPO_ROOT / "state" / "alerts.log"

POLY_API = "https://data-api.polymarket.com"
POLY_TRADES_LIMIT = 200

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ALERT_MIN_SIZE_USDC = float(os.environ.get("ALERT_MIN_SIZE_USDC", "50000"))
ALERT_NEW_MARKET_MIN_USDC = float(os.environ.get("ALERT_NEW_MARKET_MIN_USDC", "10000"))
# Alertas por POSICIÓN ACUMULADA (no por fill): captura a los que arman posiciones
# grandes con muchas compras chiquitas, sin saturar. Floor por-wallet en JSON
# (alert_position_usd); ladder de niveles a los que se notifica al cruzar.
ALERT_POSITION_USD = float(os.environ.get("ALERT_POSITION_USD", "10000"))
POSITION_LADDER = [10000, 25000, 50000, 100000, 250000, 500000, 1000000, 2500000, 5000000]
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
GMAIL_TO = os.environ.get("GMAIL_TO", GMAIL_USER).strip()

UA = {"User-Agent": "polymarket-watch/0.1 (+github actions)"}


# Categorías para filtro per-wallet. Mapeo de keywords -> categoría canónica.
# Orden importa: la primera categoría que matchee se elige. Ponemos las
# más específicas primero (Golf antes que cualquier "tournament" genérico, etc.).
CATEGORY_KEYWORDS = {
    "NBA": ["nba ","lakers","celtics","warriors","nuggets","mavericks","mavs","heat","bucks","knicks","sixers","76ers","clippers","suns","kings","timberwolves","grizzlies","spurs","jazz","blazers","trail blazers","nets","raptors","pistons","cavaliers"," cavs","hawks","wizards","magic","hornets","pelicans","bulls","thunder","rockets"],
    "NFL": ["nfl","chiefs","bills","ravens","49ers","cowboys","eagles","dolphins","jets","steelers","packers","lions","bears","vikings","falcons","saints","panthers","rams","seahawks","broncos","chargers","raiders","texans","colts","jaguars","titans","commanders","patriots","bengals","browns","buccaneers"],
    "Soccer": ["soccer","ucl","epl","uel","la liga","laliga","premier league","serie a","champions league","champions ","real madrid","barcelona","manchester","arsenal","liverpool","bayern","paris saint-germain","psg","chelsea","tottenham","city","manchester united","manchester city","manchester fc","inter milan","ac milan","internazionale","juventus","atletico","atlético","fifa","world cup","mls","alianza","fluminense","conmebol","libertadores","lyon","benfica","galatasaray","sporting cp","ajax","feyenoord","dortmund","leverkusen","schalke","wolfsburg","bundesliga","eredivisie","ligue 1","serie b","copa","sudamericana","saudi pro","saudi pro league","fc "," fc","cf ","sc ","ud ","uefa","conference league","copa america","euro 2024","euros","chivas","america","tigres","monterrey","cruz azul","pumas","liga mx"],
    "MLB": ["mlb","yankees","dodgers","mets","red sox","astros","cubs","cardinals","braves","phillies","padres","mariners","rangers","angels","brewers","reds","pirates","rockies","royals","tigers","twins","blue jays","guardians","nationals","marlins","orioles","rays","athletics","baseball","world series","alds","nlcs"],
    "NHL": ["nhl","penguins","flyers","sabres","bruins","stars","wild","oilers","leafs","maple leafs","canadiens","habs","canucks","golden knights","ducks","sharks","blackhawks","capitals","senators","hurricanes","panthers nhl","lightning","jets nhl","predators","blues","avalanche","kraken","kings la","stanley cup"],
    "Golf": ["golf","masters","pga","liv golf","ryder cup","scheffler","mcilroy","rahm","koepka","schauffele","cantlay","morikawa","spieth","justin thomas","hovland","cameron young","tony finau","tiger woods","dustin johnson","brooks","jordan spieth","collin morikawa","jon rahm","viktor hovland","xander schauffele","patrick cantlay","british open","us open golf","pga tour"],
    "Tennis": ["tennis","atp","wta","wimbledon","french open","australian open","us open tennis","djokovic","alcaraz","sinner","medvedev","zverev","tsitsipas","rune","ruud","fritz","draper","sabalenka","swiatek","gauff","rybakina"],
    "UFC/MMA": ["ufc","mma","jon jones","conor mcgregor","khabib","khamzat","bantamweight","lightweight","heavyweight","welterweight","featherweight","middleweight","fight night","mvp fight","nate diaz","mike perry","dustin poirier","islam makhachev","alex pereira","dricus du plessis"],
    "Boxing": ["boxing","canelo","fury","usyk","crawford","spence","tank davis"," joshua","ortiz vs"],
    "Cricket/Racing/Other Sports": ["cricket","ipl","f1","formula 1","grand prix","verstappen","hamilton","leclerc","norris","piastri","russell","ferrari","mclaren","red bull","racing","esports","valorant","league of legends","dota"],
    "Politics": ["election","trump","biden","kamala","harris","president","presidential","governor","senate","congress","democrat","republican","gop","primary","poll","vote","mayor","scotus","supreme court","prime minister","parliament","starmer","sunak","macron","merkel","milei","bukele","amlo","sheinbaum","next prime","next president","next chancellor","peter magyar","magyar","hungary","hungarian","brexit","cabinet","impeach","resign","approval rating"],
    "Geopolitics": ["russia","ukraine","china","iran","israel","hamas","gaza","palestine","palestinian","putin","zelensky","xi jinping","nato","war","ceasefire","peace deal","sanctions","tariff","tariffs","north korea","kim jong","strait of hormuz","hezbollah","houthi","syria","lebanon","taiwan","south china sea","drone strike","invasion","annex"],
    "Macro/Fed": ["fed ","federal reserve","fomc","interest rate","interest rates","rate cut","rate hike","inflation","cpi","ppi","unemployment","jobs report","gdp","recession","powell","yellen","s&p 500","spx","stock market","crude oil","brent","wti","oil price","gas price","ath","all time high","all-time high","dow ","nasdaq","yields","treasury","bond"],
    "Crypto": ["bitcoin","btc","ethereum"," eth","solana"," sol","xrp","dogecoin","doge","crypto","etf","halving","binance","coinbase","kraken","memecoin","stablecoin","usdc","usdt","crypto market","altcoin","ledger"],
    "Tech/AI": ["openai","gpt","anthropic","claude","ai ","artificial intelligence","apple","google","alphabet","tesla","spacex","musk","elon","nvidia","microsoft","meta ","facebook","amazon","aws","cerebras","waymo","robotaxi","sora","llama","scaling","uber","airbnb","snowflake","datadog","palantir","ipo","launch a token","launch token","starship","robot"],
    "Entertainment": ["oscar","grammy","emmy","movie","film","celebrity","box office","netflix","mrbeast","eurovision","stranger things","the bear","succession","taylor swift","beyonce","drake","kendrick","spotify","top song","streaming","disney","tv show","sequel","release","hbo","prime video","apple tv"],
    "Weather/Disasters": ["hurricane","storm","earthquake","wildfire","flood","heatwave","temperature","weather"],
}


def categorize_market(title: str) -> str:
    """Clasifica un mercado a una categoría canónica basada en keywords del título.

    Returns "Other" si ninguna keyword matchea. Sustring match en lowercase.
    Las wallets que aceptan "Other" en alert_categories captarán los no clasificados
    (útil para no perder señales mientras expandimos el diccionario).
    """
    if not title:
        return "Other"
    t = title.lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            if kw in t:
                return cat
    return "Other"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            print("[WARN] state.json corrupted, restarting clean", file=sys.stderr)
    return {"first_run": True, "wallets": {}, "last_run": None}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def load_wallets() -> list[dict]:
    return json.loads(WALLETS_PATH.read_text())


def fetch_trades(address: str, limit: int = POLY_TRADES_LIMIT) -> list[dict]:
    r = requests.get(
        f"{POLY_API}/trades",
        params={"user": address, "limit": limit},
        headers=UA,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def fetch_positions(address: str, limit: int = 500) -> list[dict]:
    """Posiciones ABIERTAS actuales del wallet (agrega los fills en una posición)."""
    r = requests.get(
        f"{POLY_API}/positions",
        params={"user": address, "limit": limit, "sizeThreshold": 1},
        headers=UA,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def position_value(p: dict) -> float:
    try:
        return float(p.get("currentValue") or 0)
    except (TypeError, ValueError):
        return 0.0


def position_shares(p: dict) -> float:
    try:
        return float(p.get("size") or 0)
    except (TypeError, ValueError):
        return 0.0


def usdc_value(trade: dict) -> float:
    """Polymarket data-api `size` is the share amount; multiply by price for USDC notional."""
    try:
        return float(trade.get("size", 0)) * float(trade.get("price", 0))
    except (TypeError, ValueError):
        return 0.0


def fmt_usd(v: float) -> str:
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}k"
    return f"${v:.0f}"


def short_addr(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def trade_event_url(trade: dict) -> str:
    slug = trade.get("eventSlug") or trade.get("slug") or ""
    return f"https://polymarket.com/event/{slug}" if slug else ""


def send_telegram(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        print("[ERROR] TELEGRAM_BOT_TOKEN/CHAT_ID not set", file=sys.stderr)
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        headers=UA,
        timeout=15,
    )
    if r.status_code != 200:
        print(f"[ERROR] Telegram {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return False
    return True


def send_email(subject: str, body: str) -> bool:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = GMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, [GMAIL_TO], msg.as_string())
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] email failed: {e}", file=sys.stderr)
        return False


def append_alert_log(line: str) -> None:
    ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERTS_LOG.open("a") as f:
        f.write(line + "\n")


def build_alert_msg(wallet_nick: str, trade: dict, kind: str, usd: float) -> str:
    title = trade.get("title", "(unknown market)")
    outcome = trade.get("outcome", "")
    side = (trade.get("side") or "?").upper()
    price = float(trade.get("price", 0))
    url = trade_event_url(trade)
    ts = trade.get("timestamp")

    # Timestamps en CDMX y UTC
    if ts:
        dt_utc = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        dt_cdmx = dt_utc.astimezone(CDMX_TZ)
        ts_str = f"{dt_cdmx.strftime('%H:%M CDMX')} ({dt_utc.strftime('%H:%M UTC')})"
    else:
        ts_str = ""

    icon = {
        "BIG_TRADE": "🔥",
        "NEW_MARKET": "🆕",
    }.get(kind, "•")

    # Identidad de Polymarket del trader (name + pseudonym de la API)
    pm_name = (trade.get("name") or "").strip()
    pm_pseudo = (trade.get("pseudonym") or "").strip()
    identity_parts = []
    if pm_name: identity_parts.append(pm_name)
    if pm_pseudo and pm_pseudo != pm_name: identity_parts.append(f'"{pm_pseudo}"')
    pm_identity = " · ".join(identity_parts) if identity_parts else ""

    # ---- header ----
    parts = [
        f"{icon} <b>{wallet_nick}</b> {side} {fmt_usd(usd)} @ {price:.3f}",
    ]
    if pm_identity:
        parts.append(f"👤 Polymarket: <b>{pm_identity}</b>")
    parts.append(f"<i>{title}</i>")
    if outcome:
        parts.append(f"Apostando a: <b>{outcome}</b>")
    if ts_str:
        parts.append(ts_str)
    if url:
        parts.append(f'<a href="{url}">→ ver mercado</a>')

    # ---- explicación didáctica ----
    parts.append("")  # línea en blanco
    parts.append("📖 <i>Cómo leerlo:</i>")
    if side == "BUY" and price > 0:
        # Calcular shares y ganancia potencial
        shares = usd / price
        max_payout = shares  # cada share paga $1 si gana
        profit_if_win = max_payout - usd
        implied_prob_pct = price * 100
        parts.append(f"• Compró <b>{fmt_usd(usd)}</b> apostando a que <b>{outcome or 'YES'}</b>")
        parts.append(f"• Pagó ${price:.2f} por share — el mercado dice prob {implied_prob_pct:.0f}%")
        parts.append(f"• Si acierta cobra ≈ {fmt_usd(max_payout)} (gana {fmt_usd(profit_if_win)})")
        parts.append(f"• Si falla pierde {fmt_usd(usd)}")
    elif side == "SELL" and price > 0:
        implied_prob_pct = price * 100
        parts.append(f"• Vendió <b>{fmt_usd(usd)}</b> de su posición en <b>{outcome or 'YES'}</b>")
        parts.append(f"• Cobró ${price:.2f} por share (mercado prob {implied_prob_pct:.0f}%)")
        parts.append(f"• Está saliendo (toma profit o corta pérdida)")
    else:
        parts.append("• Trade detectado, datos incompletos para explicación")

    return "\n".join(parts)


def build_position_alert_msg(nick: str, p: dict, kind: str, level: float, value: float, peak: float = 0.0) -> str:
    title = p.get("title", "(unknown market)")
    outcome = p.get("outcome", "")
    slug = p.get("slug") or p.get("eventSlug") or ""
    url = f"https://polymarket.com/event/{slug}" if slug else ""
    pm_name = (p.get("name") or "").strip()
    pm_pseudo = (p.get("pseudonym") or "").strip()
    ident = " · ".join([x for x in [pm_name, f'"{pm_pseudo}"' if pm_pseudo and pm_pseudo != pm_name else ""] if x])
    if kind == "BUILD":
        head = f"📈 <b>{nick}</b> posición {fmt_usd(level)}+ en mercado"
    else:  # REDUCE
        head = f"📉 <b>{nick}</b> REDUCE posición (pico {fmt_usd(peak)} → {fmt_usd(value)})"
    parts = [head]
    if ident:
        parts.append(f"👤 Polymarket: <b>{ident}</b>")
    parts.append(f"<i>{title}</i>")
    if outcome:
        parts.append(f"Apostando a: <b>{outcome}</b>")
    parts.append(f"valor actual de la posición: {fmt_usd(value)}")
    if url:
        parts.append(f'<a href="{url}">→ ver mercado</a>')
    parts.append("")
    parts.append("📖 <i>Posición acumulada (suma de sus compras), no un solo trade.</i>")
    return "\n".join(parts)


def process_positions(wallet: dict, state_w: dict, bootstrap: bool, enabled: bool, nick: str, address: str) -> list[str]:
    """Alertas por POSICIÓN ACUMULADA: cruce de niveles (BUILD) y venta fuerte (REDUCE).

    Estado por asset: {lvl: nivel más alto ya alertado, peak_val, peak_sh}.
    Bootstrap o disabled => solo registra niveles, no alerta.
    """
    floor = float(wallet.get("alert_position_usd", ALERT_POSITION_USD))
    # primera vez que corremos posiciones para este wallet => bootstrap silencioso
    # (aunque ya tenga seen_tx de la versión vieja) para no soltar una ráfaga inicial
    pos_bootstrap = bootstrap or ("positions" not in state_w)
    pos_state = state_w.get("positions", {})
    try:
        positions = fetch_positions(address)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] {nick}: positions fetch fail: {e}", file=sys.stderr)
        return []
    alerts: list[str] = []
    cur = set()
    for p in positions:
        asset = str(p.get("asset") or "")
        if not asset:
            continue
        cur.add(asset)
        val = position_value(p)
        sh = position_shares(p)
        ps = pos_state.get(asset, {"lvl": 0, "peak_val": 0.0, "peak_sh": 0.0})
        ps["peak_val"] = max(ps.get("peak_val", 0.0), val)
        ps["peak_sh"] = max(ps.get("peak_sh", 0.0), sh)
        # BUILD: el FLOOR es el primer nivel de alerta; arriba siguen los del ladder.
        ladder = [floor] + [L for L in POSITION_LADDER if L > floor]
        levels = [L for L in ladder if val >= L]
        top = levels[-1] if levels else 0
        if (not pos_bootstrap) and enabled and top > ps.get("lvl", 0):
            alerts.append(build_position_alert_msg(nick, p, "BUILD", top, val))
        ps["lvl"] = max(ps.get("lvl", 0), top)
        # REDUCE: vendió >=50% de las shares de una posición que fue grande (>= floor)
        if (not pos_bootstrap) and enabled and ps.get("peak_val", 0) >= floor \
           and ps.get("peak_sh", 0) > 0 and sh <= 0.5 * ps["peak_sh"] and not ps.get("reduce_done"):
            alerts.append(build_position_alert_msg(nick, p, "REDUCE", ps["lvl"], val, ps["peak_val"]))
            ps["reduce_done"] = True
        pos_state[asset] = ps
    # posiciones desaparecidas (resueltas/cerradas) => limpiar del estado sin alertar (resolución = ruido)
    for asset in list(pos_state.keys()):
        if asset not in cur:
            del pos_state[asset]
    # cap de tamaño
    if len(pos_state) > 1500:
        pos_state = dict(sorted(pos_state.items(), key=lambda kv: kv[1].get("peak_val", 0), reverse=True)[:1500])
    state_w["positions"] = pos_state
    return alerts


def process_wallet(wallet: dict, state_w: dict, first_run: bool) -> tuple[list[str], dict]:
    """Returns (alerts, updated_state_w).

    Per-wallet config:
    - wallet["enabled"]: si False, la wallet se procesa silenciosamente (bootstrap state) pero NO emite alertas.
    - wallet["alert_side"]: "BUY" o "SELL". Solo emite alertas para trades de ese lado. Default: ambos.
    - wallet["alert_categories"]: lista de categorías permitidas (ej. ["NBA","NHL"]). Si está vacía, todas.
    - wallet["alert_min_size_usdc"]: umbral mínimo USDC para emitir alerta BIG_TRADE.
    - wallet["alert_new_market_min_usdc"]: umbral mínimo USDC para alerta NEW_MARKET.

    Bootstrap rules:
    - first_run = True: no alerts for ANY wallet (system-wide first run)
    - first_run = False but state_w is empty: no alerts for THIS wallet
      (wallet just added — bootstrap individually to avoid spamming historic trades)
    """
    address = wallet["address"].lower()
    nick = wallet.get("nick") or short_addr(address)
    enabled = wallet.get("enabled", True)
    alert_side = (wallet.get("alert_side") or "").upper()  # "" = ambos lados
    alert_categories = wallet.get("alert_categories") or []  # [] = todas las categorías
    big_threshold = float(wallet.get("alert_min_size_usdc", ALERT_MIN_SIZE_USDC))
    new_market_threshold = float(wallet.get("alert_new_market_min_usdc", ALERT_NEW_MARKET_MIN_USDC))

    # If state_w has no seen_tx key it's brand new → bootstrap silently
    is_new_wallet = "seen_tx" not in state_w
    bootstrap = first_run or is_new_wallet

    try:
        trades = fetch_trades(address)
    except requests.HTTPError as e:
        print(f"[ERROR] {nick}: HTTP {e}", file=sys.stderr)
        return [], state_w
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {nick}: {e}", file=sys.stderr)
        return [], state_w

    seen_tx = set(state_w.get("seen_tx", []))
    seen_markets = set(state_w.get("seen_markets", []))
    new_seen_tx = list(seen_tx)
    new_seen_markets = list(seen_markets)
    alerts: list[str] = []

    if bootstrap and not first_run:
        print(f"[INFO] {nick}: new wallet, bootstrapping silently ({len(trades)} historic trades)")
    if not enabled:
        print(f"[INFO] {nick}: disabled (state preserved, no alerts)")

    # Sort oldest -> newest so notifications arrive in chronological order
    trades_sorted = sorted(trades, key=lambda t: t.get("timestamp", 0))

    for t in trades_sorted:
        tx = t.get("transactionHash")
        if not tx:
            continue
        condition_id = t.get("conditionId", "")
        if tx in seen_tx:
            # already seen, but still ensure market is recorded
            if condition_id and condition_id not in seen_markets:
                new_seen_markets.append(condition_id)
                seen_markets.add(condition_id)
            continue
        new_seen_tx.append(tx)
        usd = usdc_value(t)

        # Bootstrap or disabled: just record state, no alerts
        if bootstrap or not enabled:
            if condition_id and condition_id not in seen_markets:
                new_seen_markets.append(condition_id)
                seen_markets.add(condition_id)
            continue

        # Filter by side (only emit alerts for matching side)
        trade_side = (t.get("side") or "").upper()
        if alert_side and trade_side != alert_side:
            if condition_id and condition_id not in seen_markets:
                new_seen_markets.append(condition_id)
                seen_markets.add(condition_id)
            continue

        # Filter by category
        title = t.get("title", "")
        cat = categorize_market(title)
        if alert_categories and cat not in alert_categories:
            if condition_id and condition_id not in seen_markets:
                new_seen_markets.append(condition_id)
                seen_markets.add(condition_id)
            continue

        triggered = []
        if usd >= big_threshold:
            triggered.append(("BIG_TRADE", usd))

        new_market = bool(condition_id) and condition_id not in seen_markets
        if new_market and usd >= new_market_threshold:
            triggered.append(("NEW_MARKET", usd))

        for kind, amount in triggered:
            alerts.append(build_alert_msg(nick, t, kind, amount))

        if condition_id and condition_id not in seen_markets:
            new_seen_markets.append(condition_id)
            seen_markets.add(condition_id)

    # Alertas por POSICIÓN ACUMULADA (señal principal anti-saturación)
    alerts.extend(process_positions(wallet, state_w, bootstrap, enabled, nick, address))

    # Cap state size to last 2000 tx and 1000 markets per wallet
    state_w["seen_tx"] = new_seen_tx[-2000:]
    state_w["seen_markets"] = new_seen_markets[-1000:]
    state_w["last_seen_at"] = utc_now_iso()
    state_w["last_trade_count"] = len(trades_sorted)
    state_w["alert_min_size_usdc"] = big_threshold
    state_w["enabled"] = enabled

    return alerts, state_w


def main() -> int:
    state = load_state()
    first_run = bool(state.get("first_run"))
    wallets = load_wallets()

    if first_run:
        print(f"[INFO] first run, bootstrapping state for {len(wallets)} wallets (no alerts)")
        try:
            send_telegram(
                "🤖 <b>polymarket-watch online</b>\n"
                f"Tracking {len(wallets)} wallets. First run = bootstrap state, no alerts. "
                "Next runs will send alerts on big trades."
            )
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] hello msg failed: {e}", file=sys.stderr)

    # Prune state for wallets no longer tracked (keeps state.json clean)
    tracked_addrs = {w["address"].lower() for w in wallets}
    removed = [a for a in state["wallets"] if a not in tracked_addrs]
    for a in removed:
        del state["wallets"][a]
    if removed:
        print(f"[INFO] pruned {len(removed)} wallets no longer tracked: {', '.join(short_addr(a) for a in removed)}")

    all_alerts: list[str] = []
    for w in wallets:
        addr = w["address"].lower()
        state_w = state["wallets"].get(addr, {})
        alerts, state_w = process_wallet(w, state_w, first_run)
        state["wallets"][addr] = state_w
        all_alerts.extend(alerts)
        # courtesy delay to not hammer the API
        time.sleep(1.0)

    state["first_run"] = False
    state["last_run"] = utc_now_iso()

    # Send alerts (Telegram first, email digest as backup)
    sent = 0
    for a in all_alerts:
        if send_telegram(a):
            sent += 1
            append_alert_log(f"{utc_now_iso()} | TG_OK | {a.replace(chr(10), ' | ')}")
        else:
            append_alert_log(f"{utc_now_iso()} | TG_FAIL | {a.replace(chr(10), ' | ')}")
        time.sleep(0.4)

    if all_alerts and (GMAIL_USER and GMAIL_APP_PASSWORD):
        plain = "\n\n---\n\n".join(
            a.replace("<b>", "").replace("</b>", "")
             .replace("<i>", "").replace("</i>", "")
             .replace("<a href=\"", "").replace("\">→ open market</a>", "")
            for a in all_alerts
        )
        send_email(
            f"[polymarket-watch] {len(all_alerts)} alert(s)",
            plain,
        )

    save_state(state)

    print(f"[OK] alerts={len(all_alerts)} sent={sent} run_at={state['last_run']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

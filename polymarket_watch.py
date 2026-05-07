#!/usr/bin/env python3
"""Polymarket wallet watcher.

Cada hora (via GitHub Actions cron) revisa las wallets en tracked_wallets.json,
detecta trades nuevos vía Polymarket Data API (free, sin key), y manda alertas a
Telegram cuando hay un trade > ALERT_MIN_SIZE_USDC o cuando un wallet entra a un
mercado nuevo.

Stateless excepto por state/state.json (committed back al repo cada run).

Env vars (GitHub Secrets):
  TELEGRAM_BOT_TOKEN  - obligatorio
  TELEGRAM_CHAT_ID    - obligatorio
  ALERT_MIN_SIZE_USDC - opcional, default 50000
  GMAIL_USER          - opcional (para fallback email)
  GMAIL_APP_PASSWORD  - opcional
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

import requests

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
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
GMAIL_TO = os.environ.get("GMAIL_TO", GMAIL_USER).strip()

UA = {"User-Agent": "polymarket-watch/0.1 (+github actions)"}


# Categorias para filtro per-wallet. Mapeo de keywords -> categoría canónica.
CATEGORY_KEYWORDS = {
    "NBA": ["nba","lakers","celtics","warriors","nuggets","mavs","heat","bucks","knicks","sixers","76ers","clippers","suns","kings","timberwolves","grizzlies","spurs","jazz","blazers","nets","raptors","pistons","cavaliers","hawks","wizards","magic","hornets","pelicans","bulls","thunder","rockets"],
    "NFL": ["nfl","chiefs","bills","ravens","49ers","cowboys","eagles","dolphins","jets","steelers","packers","lions","bears","vikings","falcons","saints","panthers","rams","seahawks","broncos","chargers","raiders","texans","colts","jaguars","titans","commanders","patriots","bengals","browns","buccaneers"],
    "Soccer": ["soccer","ucl","epl","uel","la liga","laliga","premier league","serie a","champions","real madrid","barcelona","manchester","arsenal","liverpool","bayern","psg","chelsea","tottenham","city ","united","inter","milan","juventus","atletico","fifa","world cup","mls","alianza","fluminense","conmebol","libertadores","lyon","benfica","galatasaray"],
    "MLB": ["mlb","yankees","dodgers","mets","red sox","astros","cubs","cardinals","braves","phillies","padres","mariners","rangers","angels","brewers","reds","pirates","rockies","royals","tigers","twins","blue jays","guardians","nationals","marlins","orioles","rays","athletics","baseball"],
    "NHL": ["nhl","penguins","flyers","sabres","bruins","stars","wild","oilers","leafs","habs","canucks","golden knights","ducks","sharks","blackhawks","capitals","senators","hurricanes"],
    "Tennis/UFC": ["tennis","atp","wta","wimbledon","ufc","mma","djokovic","alcaraz","sinner","medvedev"],
    "Politics": ["election","trump","biden","kamala","harris","president","governor","senate","congress","democrat","republican","gop","primary","poll","vote","mayor","scotus","supreme court"],
    "Geopolitics": ["russia","ukraine","china","iran","israel","hamas","gaza","putin","nato","war","ceasefire","sanctions","tariff","north korea","strait of hormuz","hezbollah"],
    "Macro/Fed": ["fed ","fomc","interest rate","inflation","cpi","unemployment","gdp","recession","powell"],
    "Crypto": ["bitcoin","btc","ethereum","eth","solana","sol","xrp","doge","crypto","etf","halving"],
    "Tech/AI": ["openai","gpt","anthropic","claude","ai ","apple","google","tesla","spacex","musk","nvidia","microsoft"],
    "Entertainment": ["oscar","grammy","emmy","movie","film","celebrity","box office","netflix","mrbeast","eurovision"],
}


def categorize_market(title: str) -> str:
    """Clasifica un mercado a una categoría canónica basada en keywords del título."""
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
    side = trade.get("side", "?")
    price = float(trade.get("price", 0))
    pseudo = trade.get("pseudonym") or ""
    url = trade_event_url(trade)
    ts = trade.get("timestamp")
    ts_str = (
        datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%H:%M UTC")
        if ts else ""
    )
    icon = {
        "BIG_TRADE": "🔥",
        "NEW_MARKET": "🆕",
    }.get(kind, "•")
    parts = [
        f"{icon} <b>{wallet_nick}</b> {side} {fmt_usd(usd)} @ {price:.3f}",
        f"<i>{title}</i>",
    ]
    if outcome:
        parts.append(f"Outcome: <b>{outcome}</b>")
    if pseudo:
        parts.append(f"alias: {pseudo}")
    if ts_str:
        parts.append(ts_str)
    if url:
        parts.append(f'<a href="{url}">→ open market</a>')
    return "\n".join(parts)


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

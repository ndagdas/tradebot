#!/usr/bin/env python3
# ============================================================
#  BOT.PY  —  Binance Futures Long + Short Bot (HEDGE MODE)
#  Platform   : Heroku
#  TP Sistemi : 25% / 30% / 25% / 20% trail
#  NOT        : Binance Hedge Mode açık olmalı
#               Long  → positionSide="LONG"
#               Short → positionSide="SHORT"
#               Tek webhook endpoint — yön side/action'dan okunur
# ============================================================

import logging
import math
import os
import requests
from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
from binance.error import ClientError

# ── Logging ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", 5000))

# ── Lot Dağılımı ─────────────────────────────────────────────
TP1_RATIO = 0.25
TP2_RATIO = round(30 / 75, 6)   # 0.4000
TP3_RATIO = round(25 / 45, 6)   # 0.5556

# ── Binance ─────────────────────────────────────────────────
def get_client(api_key: str, api_secret: str, testnet: bool) -> UMFutures:
    if testnet:
        return UMFutures(
            key=api_key, secret=api_secret,
            base_url="https://testnet.binancefuture.com"
        )
    return UMFutures(key=api_key, secret=api_secret)

# ── Telegram ────────────────────────────────────────────────
def tg(token: str, chat: str, msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log.error(f"Telegram: {e}")

# ── Yardımcılar ─────────────────────────────────────────────
def clean_symbol(raw: str) -> str:
    s = raw.upper().strip()
    return s[:-2] if s.endswith(".P") else s

def fval(data: dict, *keys, default=0.0) -> float:
    for k in keys:
        v = data.get(k)
        if v is not None and v != "":
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return float(default)

def sval(data: dict, *keys, default="") -> str:
    for k in keys:
        v = data.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return str(default)

def parse_signal(data: dict) -> tuple[str, str]:
    """
    (action, direction) döndürür.
    action    : "open" | "tp1" | "tp2" | "tp3" | "stop"
    direction : "LONG" | "SHORT"

    Pine Script'ten gelen side değerleri:
      LONG  giriş  → side="BUY"       | action="buy"
      SHORT giriş  → side="SELL"      | action="sell"
      LONG  TP1    → side="TP1"       | action="tp1"
      SHORT TP1    → side="SHORT_TP1" | action="tp1"
      LONG  TP2    → side="TP2"       | action="tp2"
      SHORT TP2    → side="SHORT_TP2" | action="tp2"
      LONG  TP3    → side="TP3"       | action="tp3"
      SHORT TP3    → side="SHORT_TP3" | action="tp3"
      LONG  Stop   → side="STOP"      | action="stop"
      SHORT Stop   → side="SHORT_STOP"| action="stop"
    """
    action_raw = str(data.get("action", "")).strip().lower()
    side_raw   = str(data.get("side",   "")).strip().lower()

    # ── Yön belirle ──────────────────────────────────────────
    short_keywords = ("sell", "short", "short_tp1", "short_tp2",
                      "short_tp3", "short_stop")
    long_keywords  = ("buy", "long", "tp1", "tp2", "tp3",
                      "stop", "trail_update")

    if side_raw.startswith("short") or action_raw in ("sell", "short"):
        direction = "SHORT"
    else:
        direction = "LONG"

    # ── Action belirle ───────────────────────────────────────
    action_map = {
        # Long girişler
        "buy"        : "open",
        # Short girişler
        "sell"       : "open",
        "short"      : "open",
        # TP'ler
        "tp1"        : "tp1",
        "short_tp1"  : "tp1",
        "tp2"        : "tp2",
        "short_tp2"  : "tp2",
        "tp3"        : "tp3",
        "short_tp3"  : "tp3",
        # Stop / Trail
        "stop"       : "stop",
        "short_stop" : "stop",
        "trail_exit" : "stop",
        "trail_update": "trail",
        # Eski format uyumu
        "take_profit1": "tp1",
        "take_profit2": "tp2",
        "take_profit3": "tp3",
        "close"      : "stop",
    }

    # Önce side'a bak, sonra action'a
    action = action_map.get(side_raw) or action_map.get(action_raw, "")

    log.info(f"parse_signal: action_raw={action_raw} side_raw={side_raw} "
             f"→ action={action} direction={direction}")
    return action, direction

# ── Exchange Cache ───────────────────────────────────────────
_exchange_cache: dict = {}
CACHE_TTL = 300

def get_exchange_info(client: UMFutures, api_key: str,
                      force_refresh: bool = False) -> dict:
    import time
    now    = time.time()
    cached = _exchange_cache.get(api_key)
    if not force_refresh and cached and (now - cached["ts"]) < CACHE_TTL:
        log.info(f"Exchange cache hit ({int(now - cached['ts'])}s)")
        return cached["data"]
    log.info("Exchange info çekiliyor...")
    data = client.exchange_info()
    _exchange_cache[api_key] = {"data": data, "ts": now}
    return data

def _parse_symbol_info(s: dict) -> dict:
    max_qty = min_qty = None
    for f in s.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            max_qty = float(f["maxQty"])
            min_qty = float(f["minQty"])
            break
    return {
        "qty"    : s["quantityPrecision"],
        "prc"    : s["pricePrecision"],
        "max_qty": max_qty,
        "min_qty": min_qty,
    }

def get_symbol_info(client: UMFutures, symbol: str, api_key: str = "") -> dict:
    info = get_exchange_info(client, api_key)
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            return _parse_symbol_info(s)
    info = get_exchange_info(client, api_key, force_refresh=True)
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            return _parse_symbol_info(s)
    raise ValueError(f"Sembol bulunamadı: {symbol}")

def floor_qty(val: float, precision: int) -> float:
    f = 10 ** precision
    return math.floor(val * f) / f

def mark_price(client: UMFutures, symbol: str) -> float:
    return float(client.mark_price(symbol=symbol)["markPrice"])

# ── Pozisyon Sorgula ────────────────────────────────────────
def get_position(client: UMFutures, symbol: str, direction: str):
    """
    Hedge Mode ve One-Way/Testnet uyumlu pozisyon sorgusu.

    Hedge Mode:
        positionSide = "LONG" veya "SHORT"
        positionAmt  = her iki yönde pozitif

    One-Way / Testnet fallback:
        positionSide = "BOTH"
        LONG  → positionAmt > 0
        SHORT → positionAmt < 0
    """
    for p in client.get_position_risk(symbol=symbol):
        pos_side = p.get("positionSide", "BOTH")
        amt      = float(p.get("positionAmt", 0))

        # Hedge Mode
        if pos_side == direction and abs(amt) > 0:
            return p

        # One-Way / Testnet fallback
        if pos_side == "BOTH":
            if direction == "LONG"  and amt > 0:
                return p
            if direction == "SHORT" and amt < 0:
                return p

    return None

# ── Lot Yardımcısı ──────────────────────────────────────────
def safe_qty(val: float, info: dict) -> float:
    v = floor_qty(val, info["qty"])
    if info["max_qty"] and v > info["max_qty"]:
        v = floor_qty(info["max_qty"], info["qty"])
    if info["min_qty"] and v < info["min_qty"]:
        return 0.0
    return v

# ── Kısmi Kapatma ───────────────────────────────────────────
def market_close_ratio(client: UMFutures, symbol: str,
                       ratio: float, info: dict, direction: str) -> float:
    """
    Açık pozisyonun ratio kadarını kapatır.
    LONG  → SELL market
    SHORT → BUY  market
    """
    pos = get_position(client, symbol, direction)
    if not pos:
        log.info(f"Kapatma atlandı: {symbol} {direction} pozisyon yok")
        return 0.0

    total = abs(float(pos["positionAmt"]))   # SHORT testnet'te negatif gelebilir
    qty   = safe_qty(total * ratio, info)
    if qty <= 0:
        log.warning(f"Kapatma qty küçük: {symbol} qty={qty}")
        return 0.0

    close_side = "SELL" if direction == "LONG" else "BUY"
    try:
        client.new_order(
            symbol=symbol, side=close_side,
            type="MARKET", quantity=qty,
            positionSide=direction   # reduceOnly: Hedge Mode'da positionSide yeterli
        )
        log.info(f"{direction} kısmi kapat: {symbol} {qty} lot ({ratio*100:.0f}%)")
        return qty
    except Exception as e:
        log.error(f"Kısmi kapatma hatası [{symbol} {direction}]: {e}")
        return 0.0

# ── Stop Güncelle ────────────────────────────────────────────
def update_stop_order(client: UMFutures, symbol: str,
                      new_stop: float, info: dict,
                      direction: str, testnet: bool = False):
    if testnet:
        log.info(f"[TESTNET] Stop güncelleme atlandı: {symbol} @ {new_stop}")
        return

    # Mevcut STOP_MARKET emirlerini iptal et (sadece aynı yön)
    try:
        for o in client.get_orders(symbol=symbol):
            if (o.get("status") == "NEW" and
                o.get("type") == "STOP_MARKET" and
                o.get("positionSide") == direction):
                client.cancel_order(symbol=symbol, orderId=o["orderId"])
                log.info(f"Eski {direction} STOP iptal: {o['orderId']}")
    except Exception as e:
        log.warning(f"Stop iptali [{symbol} {direction}]: {e}")

    pos = get_position(client, symbol, direction)
    if not pos:
        return

    try:
        pp         = info["prc"]
        qty        = abs(float(pos["positionAmt"]))   # SHORT testnet'te negatif
        close_side = "SELL" if direction == "LONG" else "BUY"

        client.new_order(
            symbol=symbol, side=close_side, type="STOP_MARKET",
            stopPrice=round(new_stop, pp),
            quantity=qty,
            timeInForce="GTE_GTC",
            positionSide=direction   # reduceOnly: Hedge Mode'da positionSide yeterli
        )
        log.info(f"Yeni {direction} STOP: {symbol} @ {round(new_stop, pp)}")
    except Exception as e:
        log.error(f"Stop koyulamadı [{symbol} {direction}]: {e}")

# ════════════════════════════════════════════════════════════
#  LONG / SHORT AÇ
# ════════════════════════════════════════════════════════════
def open_position(client, token, chat, testnet, api_key,
                  symbol, usdt, leverage, tp1, tp2, tp3, stop,
                  direction: str):
    """
    direction = "LONG"  → BUY market + SELL TP/STOP emirleri
    direction = "SHORT" → SELL market + BUY TP/STOP emirleri
    """
    emoji = "🟢" if direction == "LONG" else "🔴"
    try:
        if get_position(client, symbol, direction):
            tg(token, chat,
               f"⚠️ <b>{symbol}</b>\nAçık {direction} var, sinyal atlandı.")
            return

        try:
            client.change_leverage(symbol=symbol, leverage=leverage)
        except ClientError:
            pass

        info     = get_symbol_info(client, symbol, api_key)
        price    = mark_price(client, symbol)
        notional = usdt * leverage
        qty      = floor_qty(notional / price, info["qty"])

        log.info(f"{direction} | {symbol} | "
                 f"{usdt}×{leverage}={notional} USDT | fiyat={price} | lot={qty}")

        if qty <= 0:
            raise ValueError(f"Lot sıfır — fiyat:{price} notional:{notional}")
        if info["max_qty"] and qty > info["max_qty"]:
            qty = floor_qty(info["max_qty"], info["qty"])
        if info["min_qty"] and qty < info["min_qty"]:
            raise ValueError(f"Min lot altında: {qty} < {info['min_qty']}")

        pp          = info["prc"]
        q           = info["qty"]
        entry_side  = "BUY"  if direction == "LONG" else "SELL"
        close_side  = "SELL" if direction == "LONG" else "BUY"

        # ── Market emri ───────────────────────────────────────
        client.new_order(
            symbol=symbol, side=entry_side,
            type="MARKET", quantity=qty,
            positionSide=direction
        )
        log.info(f"{direction} açıldı: {symbol} {qty} lot x{leverage}")

        # ── Lot hesapları ─────────────────────────────────────
        qty_tp1       = safe_qty(qty * TP1_RATIO, info)
        qty_after_tp1 = floor_qty(qty - qty_tp1, q)
        qty_tp2       = safe_qty(qty_after_tp1 * TP2_RATIO, info)
        qty_after_tp2 = floor_qty(qty_after_tp1 - qty_tp2, q)
        qty_tp3       = safe_qty(qty_after_tp2 * TP3_RATIO, info)
        qty_trail     = floor_qty(qty_after_tp2 - qty_tp3, q)

        if testnet:
            log.info(f"[TESTNET] TP emirleri atlandı: {symbol}")
            if stop > 0:
                log.info(f"[TESTNET] STOP atlandı: {symbol} @ {stop}")
        else:
            # ── TP emirleri ───────────────────────────────────
            for tp_price, tp_qty, tp_name in [
                (tp1, qty_tp1, "TP1"),
                (tp2, qty_tp2, "TP2"),
                (tp3, qty_tp3, "TP3"),
            ]:
                if tp_price > 0 and tp_qty > 0:
                    try:
                        client.new_order(
                            symbol=symbol, side=close_side,
                            type="TAKE_PROFIT_MARKET",
                            stopPrice=round(tp_price, pp),
                            quantity=tp_qty,
                            timeInForce="GTE_GTC",
                            positionSide=direction   # reduceOnly: Hedge Mode'da positionSide yeterli
                        )
                    except Exception as e:
                        log.error(f"{tp_name} emri [{symbol} {direction}]: {e}")

            # ── İlk Stop emri ─────────────────────────────────
            # closePosition Hedge Mode'da yasak → quantity kullan
            if stop > 0:
                try:
                    client.new_order(
                        symbol=symbol, side=close_side, type="STOP_MARKET",
                        stopPrice=round(stop, pp),
                        quantity=qty,
                        timeInForce="GTE_GTC",
                        positionSide=direction   # reduceOnly: Hedge Mode'da positionSide yeterli
                    )
                except Exception as e:
                    log.error(f"İlk STOP emri [{symbol} {direction}]: {e}")

        tg(token, chat,
           f"{emoji} <b>{symbol} {direction} AÇILDI</b> [Hedge Mode]\n"
           f"━━━━━━━━━━━━━━━━━\n"
           f"💰 Teminat : <b>{usdt} USDT</b>\n"
           f"⚡ Kaldıraç: <b>x{leverage}</b>\n"
           f"📊 Notional: <b>{round(notional, 2)} USDT</b>\n"
           f"📦 Toplam  : <b>{qty} lot</b>\n"
           f"💵 Giriş   : <b>{price}</b>\n"
           f"━━━━━━━━━━━━━━━━━\n"
           f"🎯 TP1 : <b>{tp1}</b>  → {qty_tp1} lot (%25)\n"
           f"🎯 TP2 : <b>{tp2}</b>  → {qty_tp2} lot (%30)\n"
           f"🎯 TP3 : <b>{tp3}</b>  → {qty_tp3} lot (%25)\n"
           f"🔄 Trail: <b>{qty_trail} lot (%20)</b>\n"
           f"🛑 Stop : <b>{stop}</b>\n"
           f"{'🔴 TESTNET' if testnet else '🟢 GERÇEK HESAP'}"
        )

    except ValueError as e:
        log.error(f"open_position [{symbol} {direction}]: {e}")
        tg(token, chat,
           f"❌ <b>{symbol} {direction} açılamadı</b>\n🔍 {e}")
    except Exception as e:
        log.error(f"open_position [{symbol} {direction}]: {e}")
        tg(token, chat,
           f"❌ <b>{symbol} {direction} açılamadı</b>\n🔍 {e}")

# ════════════════════════════════════════════════════════════
#  TP1 / TP2 / TP3
# ════════════════════════════════════════════════════════════
def handle_tp(client, token, chat, symbol, tp_num: int,
              new_stop: float, direction: str, testnet: bool,
              ratio: float, pct_label: str):
    pos = get_position(client, symbol, direction)
    if not pos:
        tg(token, chat,
           f"⚠️ <b>{symbol} TP{tp_num}</b> — {direction} pozisyon bulunamadı")
        return

    info = get_symbol_info(client, symbol)
    sold = market_close_ratio(client, symbol, ratio, info, direction)

    if new_stop > 0:
        update_stop_order(client, symbol, new_stop, info, direction, testnet)

    pos_after = get_position(client, symbol, direction)
    rem = float(pos_after["positionAmt"]) if pos_after else 0

    emoji = "🟢" if direction == "LONG" else "🔴"
    tg(token, chat,
       f"🎯 <b>{symbol} {direction} TP{tp_num} HİT</b>\n"
       f"━━━━━━━━━━━━━━━━━\n"
       f"✅ <b>{sold} lot ({pct_label})</b> kapatıldı\n"
       f"📦 Kalan: <b>{rem} lot</b>\n"
       f"🔒 Stop güncellendi: <b>{new_stop}</b>"
    )

# ════════════════════════════════════════════════════════════
#  STOP / TRAIL EXIT
# ════════════════════════════════════════════════════════════
def handle_stop(client, token, chat, symbol, direction: str):
    cancelled = 0
    close_side = "SELL" if direction == "LONG" else "BUY"

    try:
        pos = get_position(client, symbol, direction)
        if pos:
            qty = abs(float(pos["positionAmt"]))   # SHORT testnet'te negatif
            client.new_order(
                symbol=symbol, side=close_side,
                type="MARKET", quantity=qty,
                positionSide=direction   # reduceOnly: Hedge Mode'da positionSide yeterli
            )
            log.info(f"{direction} STOP: {symbol} {qty} lot kapatıldı")
    except Exception as e:
        log.warning(f"{direction} kapama [{symbol}]: {e}")

    try:
        for o in client.get_orders(symbol=symbol):
            if (o.get("status") == "NEW" and
                o.get("type") in ("TAKE_PROFIT_MARKET", "STOP_MARKET") and
                o.get("positionSide") == direction):
                client.cancel_order(symbol=symbol, orderId=o["orderId"])
                cancelled += 1
    except Exception as e:
        log.warning(f"Emir iptal [{symbol} {direction}]: {e}")

    extra = f"\n🔧 {cancelled} emir iptal edildi" if cancelled else ""
    tg(token, chat,
       f"🛑 <b>{symbol} {direction} STOP HİT</b>\n"
       f"━━━━━━━━━━━━━━━━━\n"
       f"❌ Tüm {direction} pozisyon kapatıldı{extra}"
    )

# ════════════════════════════════════════════════════════════
#  FLASK
# ════════════════════════════════════════════════════════════
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw_body = request.get_data(as_text=True)
        log.info(f"RAW: {raw_body[:500]}")

        data = request.get_json(force=True, silent=True)
        if not data:
            log.error(f"JSON okunamadı: {raw_body[:300]}")
            return jsonify({"error": "Geçersiz JSON"}), 400

        # ── Webhook Secret ────────────────────────────────────
        expected = os.environ.get("WEBHOOK_SECRET", "")
        incoming = sval(data, "webhookSecret", "webhook_secret")
        if expected and incoming != expected:
            log.warning("Geçersiz webhook secret!")
            return jsonify({"error": "Unauthorized"}), 401

        # ── Zorunlu alanlar ───────────────────────────────────
        api_key    = sval(data, "api_key",    "binanceApiKey")
        api_secret = sval(data, "api_secret", "binanceSecretKey")
        tg_token   = sval(data, "tg_token",   "telegramBotToken")
        tg_chat    = sval(data, "tg_chat_id", "telegramChatId")
        symbol     = clean_symbol(sval(data, "symbol", "ticker"))
        testnet    = sval(data, "testnet", default="true").lower() == "true"

        missing = []
        if not symbol:     missing.append("symbol/ticker")
        if not api_key:    missing.append("api_key/binanceApiKey")
        if not api_secret: missing.append("api_secret/binanceSecretKey")
        if not tg_token:   missing.append("tg_token/telegramBotToken")
        if not tg_chat:    missing.append("tg_chat_id/telegramChatId")
        if missing:
            return jsonify({"error": f"Eksik: {missing}"}), 400

        # ── Yön & Aksiyon ─────────────────────────────────────
        action, direction = parse_signal(data)
        if not action:
            return jsonify({"error": "Bilinmeyen action/side"}), 400

        log.info(f"▶ {direction} {action.upper()} | {symbol} | testnet={testnet}")
        client = get_client(api_key, api_secret, testnet)

        # ── Routing ───────────────────────────────────────────
        if action == "open":
            open_position(
                client, tg_token, tg_chat, testnet, api_key, symbol,
                usdt      = fval(data, "usdt", "quantity"),
                leverage  = int(fval(data, "leverage", default=1)),
                tp1       = fval(data, "tp1"),
                tp2       = fval(data, "tp2"),
                tp3       = fval(data, "tp3"),
                stop      = fval(data, "stop", "sl", "exitPrice", "stopPrice"),
                direction = direction
            )

        elif action == "tp1":
            handle_tp(client, tg_token, tg_chat, symbol,
                      tp_num    = 1,
                      new_stop  = fval(data, "new_stop"),
                      direction = direction,
                      testnet   = testnet,
                      ratio     = TP1_RATIO,
                      pct_label = "%25")

        elif action == "tp2":
            handle_tp(client, tg_token, tg_chat, symbol,
                      tp_num    = 2,
                      new_stop  = fval(data, "new_stop"),
                      direction = direction,
                      testnet   = testnet,
                      ratio     = TP2_RATIO,
                      pct_label = "%30")

        elif action == "tp3":
            handle_tp(client, tg_token, tg_chat, symbol,
                      tp_num    = 3,
                      new_stop  = fval(data, "new_stop"),
                      direction = direction,
                      testnet   = testnet,
                      ratio     = TP3_RATIO,
                      pct_label = "%25")

        elif action == "stop":
            handle_stop(client, tg_token, tg_chat, symbol, direction)

        elif action == "trail":
            log.info(f"Trail bilgi: {symbol} {direction} @ {fval(data, 'new_stop')}")

        else:
            return jsonify({"error": f"Bilinmeyen action: {action}"}), 400

        return jsonify({
            "status"   : "ok",
            "action"   : action,
            "direction": direction,
            "symbol"   : symbol
        }), 200

    except Exception as e:
        err_str = str(e)
        if "-1121" in err_str:
            log.warning(f"Geçersiz sembol atlandı: {err_str[:80]}")
            return jsonify({"status": "skipped", "reason": "invalid_symbol"}), 200
        log.error(f"Webhook hatası: {e}")
        return jsonify({"error": err_str}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status"  : "running",
        "mode"    : "LONG+SHORT",
        "hedge"   : True,
        "platform": "heroku"
    }), 200

if __name__ == "__main__":
    log.info(f"Long+Short Bot başlatıldı | Port: {PORT} | Hedge Mode: ON")
    app.run(host="0.0.0.0", port=PORT, debug=False)

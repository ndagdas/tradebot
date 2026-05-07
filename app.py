#!/usr/bin/env python3
# ============================================================
#  BOT.PY  —  Binance Futures Long Bot
#  Platform   : Heroku
#  TP Sistemi : 25% / 30% / 25% / 20% trail
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
# Toplam 100 lot üzerinden:
#   TP1 → 25 lot  (%25)
#   TP2 → 30 lot  (%30)   [kalan 75'in %40'ı]
#   TP3 → 25 lot  (%25)   [kalan 45'in %55.6'sı]
#   Trail → 20 lot (%20)   [kalan 20 sürüklenir]
TP1_RATIO = 0.25
TP2_RATIO = round(30 / 75, 6)    # 0.4
TP3_RATIO = round(25 / 45, 6)    # 0.5556

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

def parse_action(data: dict) -> str:
    action = str(data.get("action", "")).strip().lower()
    if action in ("buy", "tp1", "tp2", "tp3", "stop", "trail_update"):
        return action

    side = str(data.get("side", "")).strip().lower()
    side_map = {
        "buy"  : "buy",  "long"         : "buy",
        "sell" : "stop", "short"        : "stop",
        "stop" : "stop", "close"        : "stop",
        "tp1"  : "tp1",  "take_profit1" : "tp1",
        "tp2"  : "tp2",  "take_profit2" : "tp2",
        "tp3"  : "tp3",  "take_profit3" : "tp3",
        "trail_update": "trail_update",
    }
    if side in side_map:
        exit_type = str(data.get("exitType", "")).strip().lower()
        exit_map  = {
            "tp1_exit"   : "tp1",
            "tp2_exit"   : "tp2",
            "tp3_exit"   : "tp3",
            "trail_exit" : "stop",
        }
        if exit_type in exit_map:
            return exit_map[exit_type]
        return side_map[side]
    return action

# ── Exchange Cache ───────────────────────────────────────────
_exchange_cache: dict = {}
CACHE_TTL = 300

def get_exchange_info(client: UMFutures, api_key: str, force_refresh: bool = False) -> dict:
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

def _parse_symbol(s: dict) -> dict:
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
            return _parse_symbol(s)
    log.warning(f"{symbol} cache'de yok, taze çekiliyor...")
    info = get_exchange_info(client, api_key, force_refresh=True)
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            return _parse_symbol(s)
    raise ValueError(f"Binance Futures'da sembol bulunamadı: {symbol}")

def floor_qty(val: float, precision: int) -> float:
    f = 10 ** precision
    return math.floor(val * f) / f

def mark_price(client: UMFutures, symbol: str) -> float:
    return float(client.mark_price(symbol=symbol)["markPrice"])

def open_position(client: UMFutures, symbol: str):
    for p in client.get_position_risk(symbol=symbol):
        if float(p["positionAmt"]) > 0:
            return p
    return None

# ── LONG AÇ ─────────────────────────────────────────────────
def open_long(client, token, chat, testnet, api_key,
              symbol, usdt, leverage, tp1, tp2, tp3, stop):
    try:
        if open_position(client, symbol):
            tg(token, chat, f"⚠️ <b>{symbol}</b>\nAçık pozisyon var, sinyal atlandı.")
            return

        try:
            client.change_leverage(symbol=symbol, leverage=leverage)
        except ClientError:
            pass

        info     = get_symbol_info(client, symbol, api_key)
        price    = mark_price(client, symbol)
        notional = usdt * leverage
        qty      = floor_qty(notional / price, info["qty"])

        log.info(f"Hesap: {usdt}×{leverage}={notional} USDT | Fiyat:{price} | Lot:{qty}")

        if qty <= 0:
            raise ValueError(f"Lot sıfır — fiyat:{price} notional:{notional}")
        if info["max_qty"] and qty > info["max_qty"]:
            log.warning(f"Max lot kırpıldı: {qty} → {info['max_qty']}")
            qty = floor_qty(info["max_qty"], info["qty"])
        if info["min_qty"] and qty < info["min_qty"]:
            raise ValueError(f"Min lot altında: {qty} < {info['min_qty']}")

        pp     = info["prc"]
        q      = info["qty"]
        maxq   = info["max_qty"]

        def safe_qty(val):
            """Lot değerini max_qty ile kırp, min_qty'nin altındaysa 0 döndür."""
            v = floor_qty(val, q)
            if maxq and v > maxq:
                v = floor_qty(maxq, q)
            if info["min_qty"] and v < info["min_qty"]:
                return 0.0
            return v

        # Market emri
        client.new_order(symbol=symbol, side="BUY", type="MARKET", quantity=qty)
        log.info(f"Market emri açıldı: {symbol} {qty} lot")

        qty_tp1       = safe_qty(qty * TP1_RATIO)
        qty_after_tp1 = floor_qty(qty - qty_tp1, q)
        qty_tp2       = safe_qty(qty_after_tp1 * TP2_RATIO)
        qty_after_tp2 = floor_qty(qty_after_tp1 - qty_tp2, q)
        qty_tp3       = safe_qty(qty_after_tp2 * TP3_RATIO)

        if testnet:
            # Testnet'te TAKE_PROFIT_MARKET desteklenmiyor.
            # Pine TP1/TP2/TP3 sinyali gönderince bot market satış yapacak.
            log.info(f"[TESTNET] TP emirleri atlandı, Pine sinyali ile satılacak: {symbol}")
        else:
            # ── TP1: %25 ──────────────────────────────────────────
            if tp1 > 0 and qty_tp1 > 0:
                try:
                    client.new_order(
                        symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET",
                        stopPrice=round(tp1, pp), quantity=qty_tp1,
                        timeInForce="GTE_GTC", reduceOnly="true"
                    )
                except Exception as e:
                    log.error(f"TP1 emri hatası [{symbol}]: {e}")

            # ── TP2: %30 (kalanın %40'ı) ──────────────────────────
            if tp2 > 0 and qty_tp2 > 0:
                try:
                    client.new_order(
                        symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET",
                        stopPrice=round(tp2, pp), quantity=qty_tp2,
                        timeInForce="GTE_GTC", reduceOnly="true"
                    )
                except Exception as e:
                    log.error(f"TP2 emri hatası [{symbol}]: {e}")

            # ── TP3: %25 (kalanın %55.6'sı) ───────────────────────
            if tp3 > 0 and qty_tp3 > 0:
                try:
                    client.new_order(
                        symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET",
                        stopPrice=round(tp3, pp), quantity=qty_tp3,
                        timeInForce="GTE_GTC", reduceOnly="true"
                    )
                except Exception as e:
                    log.error(f"TP3 emri hatası [{symbol}]: {e}")

        # ── STOP: kalan %20 trail için ─────────────────────────
        # Testnet STOP_MARKET desteklemiyor, gerçek hesapta gönder
        if stop > 0 and not testnet:
            client.new_order(
                symbol=symbol, side="SELL", type="STOP_MARKET",
                stopPrice=round(stop, pp), closePosition="true",
                timeInForce="GTE_GTC"
            )
        elif stop > 0 and testnet:
            log.info(f"[TESTNET] İlk STOP emri atlandı: {symbol} @ {stop}")

        qty_trail = floor_qty(qty_after_tp2 - qty_tp3, q)
        log.info(
            f"LONG açıldı: {symbol} {qty} lot x{leverage}\n"
            f"  TP1={tp1} ({qty_tp1} lot) | TP2={tp2} ({qty_tp2} lot) | "
            f"TP3={tp3} ({qty_tp3} lot) | Trail={qty_trail} lot | Stop={stop}"
        )

        tg(token, chat,
           f"🟢 <b>{symbol} LONG AÇILDI</b>\n"
           f"━━━━━━━━━━━━━━━━━\n"
           f"💰 Teminat : <b>{usdt} USDT</b>\n"
           f"⚡ Kaldıraç: <b>x{leverage}</b>\n"
           f"📊 Notional: <b>{round(notional,2)} USDT</b>\n"
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
        log.error(f"open_long [{symbol}]: {e}")
        tg(token, chat, f"❌ <b>{symbol} LONG açılamadı</b>\n🔍 {e}")
    except Exception as e:
        log.error(f"open_long [{symbol}]: {e}")
        tg(token, chat, f"❌ <b>{symbol} LONG açılamadı</b>\n🔍 {e}")

# ── STOP GÜNCELLE ────────────────────────────────────────────
def update_stop_order(client, symbol, new_stop_price: float, info: dict, testnet: bool = False):
    """
    Mevcut STOP_MARKET emrini iptal et, yeni fiyatla tekrar koy.
    Testnet STOP_MARKET emrini standart endpoint'te desteklemiyor (-4120).
    Testnet'te sadece loglanır, gerçek hesapta emir gönderilir.
    """
    if testnet:
        log.info(f"[TESTNET] Stop güncelleme atlandı (testnet -4120): {symbol} @ {new_stop_price}")
        return

    # Önce bekleyen STOP emirlerini iptal et
    try:
        orders = client.get_orders(symbol=symbol)
        for o in orders:
            if o.get("status") == "NEW" and o.get("type") == "STOP_MARKET":
                client.cancel_order(symbol=symbol, orderId=o["orderId"])
                log.info(f"Eski STOP iptal: {o['orderId']}")
    except Exception as e:
        log.warning(f"Stop iptali [{symbol}]: {e}")

    # Açık pozisyon miktarını al
    pos = open_position(client, symbol)
    if not pos:
        log.info(f"Stop güncelleme atlandı: {symbol} pozisyon kapalı")
        return

    try:
        pp  = info["prc"]
        qty = float(pos["positionAmt"])
        client.new_order(
            symbol=symbol, side="SELL", type="STOP_MARKET",
            stopPrice=round(new_stop_price, pp),
            quantity=qty,
            timeInForce="GTE_GTC",
            reduceOnly="true"
        )
        log.info(f"Yeni STOP: {symbol} @ {round(new_stop_price, pp)} qty={qty}")
    except Exception as e:
        log.error(f"Yeni stop koyulamadı [{symbol}]: {e}")

# ── Yardımcı: Pozisyonun belirli oranını market ile sat ──────
def market_sell_ratio(client, symbol, ratio: float, info: dict) -> float:
    """
    Açık pozisyonun ratio kadarını market emriyle sat.
    Satılan lot miktarını döndürür, hata olursa 0.
    """
    pos = open_position(client, symbol)
    if not pos:
        log.info(f"Satış atlandı: {symbol} pozisyon yok")
        return 0.0
    total = float(pos["positionAmt"])
    qty   = floor_qty(total * ratio, info["qty"])
    if info["max_qty"] and qty > info["max_qty"]:
        qty = floor_qty(info["max_qty"], info["qty"])
    if not qty or qty < (info["min_qty"] or 0):
        log.warning(f"Satış qty çok küçük: {symbol} qty={qty}")
        return 0.0
    try:
        client.new_order(
            symbol=symbol, side="SELL",
            type="MARKET", quantity=qty,
            reduceOnly="true"
        )
        log.info(f"Market sat: {symbol} {qty} lot ({ratio*100:.0f}%)")
        return qty
    except Exception as e:
        log.error(f"Market satış hatası [{symbol}]: {e}")
        return 0.0


# ── TP1 ──────────────────────────────────────────────────────
def handle_tp1(client, token, chat, symbol, new_stop: float = 0, testnet: bool = False):
    pos = open_position(client, symbol)
    if not pos:
        tg(token, chat, f"⚠️ <b>{symbol} TP1</b> — Pozisyon bulunamadı")
        return

    info = get_symbol_info(client, symbol)

    # %25 market ile sat
    sold = market_sell_ratio(client, symbol, TP1_RATIO, info)

    # Stop BE'ye çek (gerçek hesapta)
    if new_stop > 0:
        update_stop_order(client, symbol, new_stop, info, testnet)

    pos_after = open_position(client, symbol)
    rem = float(pos_after["positionAmt"]) if pos_after else 0

    tg(token, chat,
       f"🎯 <b>{symbol} TP1 HİT</b>\n"
       f"━━━━━━━━━━━━━━━━━\n"
       f"✅ <b>{sold} lot (%25)</b> satıldı\n"
       f"📦 Kalan: <b>{rem} lot</b>\n"
       f"🔒 Stop BE'ye çekildi: <b>{new_stop}</b>"
    )

# ── TP2 ──────────────────────────────────────────────────────
def handle_tp2(client, token, chat, symbol, new_stop: float = 0, testnet: bool = False):
    pos = open_position(client, symbol)
    if not pos:
        tg(token, chat, f"⚠️ <b>{symbol} TP2</b> — Pozisyon bulunamadı")
        return

    info = get_symbol_info(client, symbol)

    # Kalan pozisyonun %40'ını sat (toplam %30)
    sold = market_sell_ratio(client, symbol, TP2_RATIO, info)

    if new_stop > 0:
        update_stop_order(client, symbol, new_stop, info, testnet)

    pos_after = open_position(client, symbol)
    rem = float(pos_after["positionAmt"]) if pos_after else 0

    tg(token, chat,
       f"🎯 <b>{symbol} TP2 HİT</b>\n"
       f"━━━━━━━━━━━━━━━━━\n"
       f"✅ <b>{sold} lot (%30)</b> satıldı\n"
       f"📦 Kalan: <b>{rem} lot</b>\n"
       f"🔒 Stop TP1 seviyesine çekildi: <b>{new_stop}</b>"
    )

# ── TP3 ──────────────────────────────────────────────────────
def handle_tp3(client, token, chat, symbol, new_stop: float = 0, testnet: bool = False):
    pos = open_position(client, symbol)
    if not pos:
        tg(token, chat, f"⚠️ <b>{symbol} TP3</b> — Pozisyon bulunamadı")
        return

    info = get_symbol_info(client, symbol)

    # Kalan pozisyonun %55.6'sını sat (toplam %25)
    sold = market_sell_ratio(client, symbol, TP3_RATIO, info)

    if new_stop > 0:
        update_stop_order(client, symbol, new_stop, info, testnet)

    pos_after = open_position(client, symbol)
    rem = float(pos_after["positionAmt"]) if pos_after else 0

    tg(token, chat,
       f"🎯 <b>{symbol} TP3 HİT</b>\n"
       f"━━━━━━━━━━━━━━━━━\n"
       f"✅ <b>{sold} lot (%25)</b> satıldı\n"
       f"📦 Kalan: <b>{rem} lot (trail)</b>\n"
       f"🔄 Trailing stop aktif: <b>{new_stop}</b>"
    )

# ── TRAIL UPDATE ─────────────────────────────────────────────
def handle_trail_update(client, token, chat, symbol, new_stop: float):
    """
    Pine Script trail stop seviyesini her barda gönderir.
    Pine kendi içinde trail mantığını yönetiyor —
    stop tetiklenince ayrıca 'stop' sinyali gönderir.
    Burada Binance'e dokunmaya gerek yok, sadece 200 dön.
    """
    log.info(f"Trail bilgi: {symbol} @ {new_stop} (Binance emri güncellenmedi)")

# ── STOP ─────────────────────────────────────────────────────
def handle_stop(client, token, chat, symbol):
    cancelled = 0
    try:
        pos = open_position(client, symbol)
        if pos:
            qty = float(pos["positionAmt"])
            client.new_order(
                symbol=symbol, side="SELL", type="MARKET",
                quantity=qty, reduceOnly="true"
            )
            log.info(f"STOP: {symbol} market kapatıldı ({qty} lot)")
    except Exception as e:
        log.warning(f"STOP market kapat [{symbol}]: {e}")
    try:
        for o in client.get_orders(symbol=symbol):
            if o.get("status") == "NEW" and o.get("type") in ("TAKE_PROFIT_MARKET", "STOP_MARKET"):
                client.cancel_order(symbol=symbol, orderId=o["orderId"])
                cancelled += 1
    except Exception as e:
        log.warning(f"Emir iptal [{symbol}]: {e}")
    extra = f"\n🔧 {cancelled} emir iptal edildi" if cancelled else ""
    tg(token, chat,
       f"🛑 <b>{symbol} STOP HİT</b>\n"
       f"━━━━━━━━━━━━━━━━━\n"
       f"❌ Tüm pozisyon kapatıldı{extra}"
    )

# ── FLASK ────────────────────────────────────────────────────
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

        action = parse_action(data)
        symbol = clean_symbol(sval(data, "symbol", "ticker"))

        api_key    = sval(data, "api_key",    "binanceApiKey")
        api_secret = sval(data, "api_secret", "binanceSecretKey")
        tg_token   = sval(data, "tg_token",   "telegramBotToken")
        tg_chat    = sval(data, "tg_chat_id", "telegramChatId")
        testnet    = sval(data, "testnet", default="true").lower() == "true"

        # ── Webhook Secret doğrulama ──────────────────────────
        expected_secret = os.environ.get("WEBHOOK_SECRET", "")
        incoming_secret = sval(data, "webhookSecret", "webhook_secret")
        if expected_secret and incoming_secret != expected_secret:
            log.warning(f"Geçersiz webhook secret!")
            return jsonify({"error": "Unauthorized"}), 401

        missing = []
        if not action:     missing.append("action/side")
        if not symbol:     missing.append("symbol/ticker")
        if not api_key:    missing.append("api_key/binanceApiKey")
        if not api_secret: missing.append("api_secret/binanceSecretKey")
        if not tg_token:   missing.append("tg_token/telegramBotToken")
        if not tg_chat:    missing.append("tg_chat_id/telegramChatId")

        if missing:
            log.error(f"Eksik: {missing} | Data: {data}")
            return jsonify({"error": f"Eksik alanlar: {missing}"}), 400

        log.info(f"▶ {action.upper()} | {symbol} | testnet={testnet}")
        client = get_client(api_key, api_secret, testnet)

        if action == "buy":
            open_long(
                client, tg_token, tg_chat, testnet, api_key, symbol,
                usdt     = fval(data, "usdt", "quantity"),
                leverage = int(fval(data, "leverage", default=1)),
                tp1      = fval(data, "tp1"),
                tp2      = fval(data, "tp2"),
                tp3      = fval(data, "tp3"),
                stop     = fval(data, "stop", "sl", "exitPrice", "stopPrice")
            )
        elif action == "tp1":
            handle_tp1(client, tg_token, tg_chat, symbol,
                       new_stop=fval(data, "new_stop"), testnet=testnet)
        elif action == "tp2":
            handle_tp2(client, tg_token, tg_chat, symbol,
                       new_stop=fval(data, "new_stop"), testnet=testnet)
        elif action == "tp3":
            handle_tp3(client, tg_token, tg_chat, symbol,
                       new_stop=fval(data, "new_stop"), testnet=testnet)
        elif action == "trail_update":
            handle_trail_update(client, tg_token, tg_chat, symbol,
                                new_stop=fval(data, "new_stop"))
        elif action == "stop":
            handle_stop(client, tg_token, tg_chat, symbol)
        else:
            return jsonify({"error": f"Bilinmeyen action: {action}"}), 400

        return jsonify({"status": "ok", "action": action, "symbol": symbol}), 200

    except Exception as e:
        err_str = str(e)
        # Binance -1121: geçersiz sembol — kullanılmayan sinyal, 200 dön
        if "-1121" in err_str:
            log.warning(f"Geçersiz sembol, atlandı: {err_str[:80]}")
            return jsonify({"status": "skipped", "reason": "invalid_symbol"}), 200
        log.error(f"Webhook hatası: {e}")
        return jsonify({"error": err_str}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "running", "platform": "heroku"}), 200

if __name__ == "__main__":
    log.info(f"Bot başlatıldı | Port: {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)

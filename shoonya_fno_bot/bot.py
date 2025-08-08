import os
import sys
import time
import math
import hmac
import base64
import struct
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pytz
from dateutil import tz
from dotenv import load_dotenv

# Try multiple available wrappers to improve portability
ApiClass = None
_api_import_error = None
try:
    from NorenRestApi.NorenApi import NorenApi as ApiClass  # type: ignore
except Exception as e1:
    _api_import_error = e1
    try:
        from NorenRestApiPy.NorenApi import NorenApi as ApiClass  # type: ignore
    except Exception as e2:
        _api_import_error = (e1, e2)
        try:
            from api_helper import ShoonyaApiPy as ApiClass  # type: ignore
        except Exception as e3:
            _api_import_error = (e1, e2, e3)
            ApiClass = None

# Optional OAuth helpers
OAuthApiClass = None
try:
    from NorenRestApiPy.api_helper import NorenApi as OAuthApiClass  # type: ignore
except Exception:
    pass

IST = pytz.timezone("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def totp_now(secret_b32: str, interval: int = 30, digits: int = 6) -> str:
    secret = base64.b32decode(secret_b32.upper())
    counter = int(time.time() // interval)
    msg = struct.pack('>Q', counter)
    h = hmac.new(secret, msg, hashlib.sha1).digest()
    o = h[19] & 15
    code = (struct.unpack('>I', h[o:o+4])[0] & 0x7fffffff) % (10 ** digits)
    return f"{code:0{digits}d}"


class ShoonyaClient:
    def __init__(self) -> None:
        if ApiClass is None:
            raise ImportError(
                f"Shoonya API import failed. Tried NorenRestApi.NorenApi, NorenRestApiPy.NorenApi, api_helper.ShoonyaApiPy. Errors: {_api_import_error}"
            )
        host = os.getenv("SHOONYA_HOST", "https://api.shoonya.com/NorenWClient/" )
        ws   = os.getenv("SHOONYA_WEBSOCKET", "wss://api.shoonya.com/NorenWSTp/" )
        try:
            self.api = ApiClass(host, ws)
        except TypeError:
            # Some variants accept keywords
            self.api = ApiClass(host=host, websocket=ws)
        self.uid: Optional[str] = None
        self.account_id: Optional[str] = None

    def login(self) -> None:
        uid = os.getenv("SHOONYA_USER")
        pwd = os.getenv("SHOONYA_PASSWORD")
        vc = os.getenv("SHOONYA_VENDOR_CODE")
        app_key = os.getenv("SHOONYA_API_KEY")
        imei = os.getenv("SHOONYA_IMEI")
        totp_secret = os.getenv("SHOONYA_TOTP_SECRET", "").strip()
        oauth_url = os.getenv("SHOONYA_OAUTH_URL", "").strip()
        secret_key = os.getenv("SHOONYA_SECRET_KEY", "").strip()
        auth_code = os.getenv("SHOONYA_AUTH_CODE", "").strip()

        if not uid or not pwd:
            raise RuntimeError("Missing SHOONYA_USER or SHOONYA_PASSWORD")

        twoFA = totp_now(totp_secret) if totp_secret else os.getenv("SHOONYA_OTP")
        if not twoFA:
            raise RuntimeError("Provide either SHOONYA_TOTP_SECRET or one-time SHOONYA_OTP in env")

        # Try OAuth path first if provided
        if oauth_url and secret_key and app_key and OAuthApiClass is not None:
            try:
                oauth_api = OAuthApiClass()
                if not auth_code:
                    # Just guide user to URL (cannot open browser here)
                    print("Open OAuth URL in a browser, log in, and paste the 'code' into SHOONYA_AUTH_CODE in .env:")
                    print(oauth_url)
                    raise RuntimeError("Missing SHOONYA_AUTH_CODE for OAuth flow")
                acc_tok, usrid, ref_tok, actid = oauth_api.getAccessToken(auth_code, secret_key, app_key, uid)
                oauth_api.injectOAuthHeader(acc_tok, uid, actid)
                # Replace self.api with oauth_api-compatible object if needed
                self.api = oauth_api  # type: ignore
                self.uid = uid
                self.account_id = actid
                print(f"OAuth login complete for {uid}")
                return
            except Exception as e:
                print(f"OAuth login attempt failed: {e}")

        # Try multiple signatures depending on what's available
        attempts = []
        if vc and app_key and imei:
            attempts.append(dict(userid=uid, password=pwd, twoFA=twoFA, vendor_code=vc, api_secret=app_key, imei=imei))
        if app_key:
            attempts.append(dict(userid=uid, password=pwd, twoFA=twoFA, api_secret=app_key))
        attempts.append(dict(userid=uid, password=pwd, twoFA=twoFA))

        last_err = None
        ret = None
        for params in attempts:
            try:
                ret = self.api.login(**params)
                if ret and ret.get("stat") == "Ok":
                    break
            except Exception as e:
                last_err = e
                ret = None
        if not ret or ret.get("stat") != "Ok":
            raise RuntimeError(f"Login failed: {ret or last_err}")

        self.uid = uid
        self.account_id = ret.get("actid") or ret.get("actid", uid)
        print(f"Logged in as {uid}")

    def logout(self) -> None:
        try:
            self.api.logout()
        except Exception:
            pass

    # --- Market helpers ---
    def search(self, exch: str, text: str) -> List[Dict]:
        res = self.api.searchscrip(exchange=exch, searchtext=text)
        if not res or res.get("stat") != "Ok":
            return []
        return res.get("values", [])

    def get_security_info(self, exch: str, token: str) -> Optional[Dict]:
        res = self.api.get_security_info(exchange=exch, token=token)
        if res and res.get("stat") == "Ok":
            return res
        return None

    def get_5m_candles_today(self, exch: str, token: str) -> List[Dict]:
        # From 09:15 IST today till now
        today = now_ist().date()
        start_dt = IST.localize(datetime(today.year, today.month, today.day, 9, 15, 0))
        end_dt = now_ist()
        start_epoch = int(start_dt.timestamp())
        end_epoch = int(end_dt.timestamp())

        ret = None
        try:
            ret = self.api.get_time_price_series(exchange=exch, token=token, starttime=start_epoch, endtime=end_epoch, interval=5)
        except TypeError:
            ret = self.api.get_time_price_series(exchange=exch, token=token, starttime=str(start_epoch), endtime=str(end_epoch), interval=5)

        if not ret or ret.get("stat") != "Ok":
            raise RuntimeError(f"get_time_price_series failed for {exch}:{token} => {ret}")

        rows = ret.get("candles") or ret.get("values") or []
        candles: List[Dict] = []

        def parse_ts(x: str) -> datetime:
            try:
                dt = datetime.strptime(str(x), "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    dt = datetime.strptime(str(x), "%d-%m-%Y %H:%M:%S")
                except Exception:
                    dt = datetime.fromtimestamp(int(x), tz=IST).replace(tzinfo=None)
            return IST.localize(dt)

        for r in rows:
            # Expected [time, o, h, l, c, v]
            if len(r) < 5:
                continue
            t = parse_ts(r[0])
            o = float(r[1])
            h = float(r[2])
            l = float(r[3])
            c = float(r[4])
            v = float(r[5]) if len(r) > 5 and r[5] is not None else 0.0
            candles.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v})

        return candles

    # --- Orders ---
    def place_market_buy(self, exch: str, tsym: str, qty: int, prd: str, remarks: str = "") -> Dict:
        return self.api.place_order(
            buy_or_sell='B', product_type=prd,
            exchange=exch, tradingsymbol=tsym,
            quantity=qty, discloseqty=0, price_type='MKT', price=0.0,
            retention='DAY', remarks=remarks or 'fno-bot'
        )

    def place_market_sell(self, exch: str, tsym: str, qty: int, prd: str, remarks: str = "") -> Dict:
        return self.api.place_order(
            buy_or_sell='S', product_type=prd,
            exchange=exch, tradingsymbol=tsym,
            quantity=qty, discloseqty=0, price_type='MKT', price=0.0,
            retention='DAY', remarks=remarks or 'fno-bot-exit'
        )

    def get_ltp(self, exch: str, token: Optional[str], tsym: Optional[str]) -> Optional[float]:
        try:
            if token:
                q = self.api.get_quotes(exch, token)
            else:
                # fallback if token missing
                vals = self.search(exch, tsym or "")
                tok = vals[0].get('token') if vals else None
                if not tok:
                    return None
                q = self.api.get_quotes(exch, tok)
            if q and q.get('stat') == 'Ok':
                lp = q.get('lp') or q.get('last_price')
                return float(lp)
        except Exception:
            return None
        return None


# --- Technical indicators on lists ---

def latest_rsi_two_ago(closes: List[float], period: int = 14) -> Optional[float]:
    n = len(closes)
    if n < period + 3:
        return None
    # Compute RSI for the third last point using window ending at index n-3
    end_idx = n - 3
    start_idx = end_idx - period
    window = closes[start_idx:end_idx+1]
    deltas = [window[i] - window[i-1] for i in range(1, len(window))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def wr_values_for_indices(highs: List[float], lows: List[float], closes: List[float], idx_list: List[int], period: int = 14) -> Dict[int, Optional[float]]:
    result: Dict[int, Optional[float]] = {}
    for idx in idx_list:
        if idx - period + 1 < 0:
            result[idx] = None
            continue
        window_high = max(highs[idx - period + 1: idx + 1])
        window_low = min(lows[idx - period + 1: idx + 1])
        denom = (window_high - window_low)
        if denom <= 0:
            result[idx] = None
        else:
            result[idx] = -100.0 * (window_high - closes[idx]) / denom
    return result


def condition_prev_low_equals_todays_low(lows: List[float], tick_size: float) -> bool:
    if len(lows) < 2:
        return False
    prev_low = float(lows[-2])
    today_low = float(min(lows))
    return abs(prev_low - today_low) <= max(tick_size, 1e-6)


def condition_wr_crossed_up(highs: List[float], lows: List[float], closes: List[float]) -> bool:
    n = len(closes)
    if n < 3 + 14:
        return False
    idx_prev = n - 2  # one bar ago
    idx_two = n - 3   # two bars ago
    wrs = wr_values_for_indices(highs, lows, closes, [idx_two, idx_prev], period=14)
    w_two = wrs.get(idx_two)
    w_prev = wrs.get(idx_prev)
    if w_two is None or w_prev is None:
        return False
    return (w_two < -80.0) and (w_prev > -80.0)


def condition_rsi_two_ago_below_20(closes: List[float]) -> bool:
    r = latest_rsi_two_ago(closes, period=14)
    return (r is not None) and (r < 20.0)


def main() -> None:
    load_dotenv()

    watchlist = [s.strip().upper() for s in os.getenv('WATCHLIST', '').split(',') if s.strip()]
    if not watchlist:
        print('WATCHLIST env is empty. Example: WATCHLIST=RELIANCE, HDFCBANK, TCS')
        sys.exit(1)

    product_type = os.getenv('PRODUCT_TYPE', 'I').upper()
    trade_segment = os.getenv('TRADE_SEGMENT', 'EQ').upper()
    qty_per_order = int(os.getenv('QUANTITY_PER_ORDER', '10'))
    lots_per_order = int(os.getenv('LOTS_PER_ORDER', '1'))
    once_per_day = os.getenv('ONCE_PER_DAY', 'true').lower() == 'true'
    poll_interval = int(os.getenv('POLL_INTERVAL_SEC', '20'))

    client = ShoonyaClient()
    try:
        client.login()
    except Exception as e:
        print(f"Login failed: {e}")
        print("Please ensure you set SHOONYA_VENDOR_CODE, SHOONYA_IMEI, and either SHOONYA_TOTP_SECRET or SHOONYA_OTP in .env")
        sys.exit(1)

    try:
        # Resolve contracts
        contracts: Dict[str, Dict] = {}
        if trade_segment == 'EQ':
            # Resolve equity symbols on NSE (tradingsymbol usually like RELIANCE-EQ)
            for sym in watchlist:
                # search exact equity tsym
                vals = client.search('NSE', sym)
                if not vals:
                    print(f"No EQ found for {sym}")
                    continue
                # pick first NSE equity match with -EQ if present
                chosen = None
                for v in vals:
                    tsym = (v.get('tsym') or '').upper()
                    if tsym.endswith('-EQ') and v.get('exch') == 'NSE':
                        chosen = v
                        break
                if not chosen:
                    chosen = vals[0]
                chosen['exch'] = chosen.get('exch', 'NSE')
                chosen['tsym'] = chosen.get('tsym')
                # Defaults for tick and lot
                chosen['lot_size'] = 1
                chosen['tick_size'] = float(chosen.get('ti') or 0.05)
                contracts[sym] = chosen
                print(f"{sym} -> {chosen['tsym']} (EQ)")
        else:
            for sym in watchlist:
                res = find_nearest_month_fut(client, sym)
                if not res:
                    print(f"No FUT contract found for {sym}")
                    continue
                exch, tsym, info = res
                info['exch'] = exch
                info['tsym'] = tsym
                lot_size = int((info.get('ls') or info.get('lotsize') or 1))
                tick_size = float((info.get('ti') or info.get('tick_size') or 0.05))
                info['lot_size'] = lot_size
                info['tick_size'] = tick_size
                contracts[sym] = info
                print(f"{sym} -> {tsym} lot={lot_size} tick={tick_size}")

        if not contracts:
            print('No tradable contracts resolved. Exiting.')
            return

        # Per-symbol state
        last_bar_time: Dict[str, datetime] = {}
        bought_today: Dict[str, bool] = {s: False for s in contracts}
        open_positions: Dict[str, Dict] = {}

        # Trading window: 09:45 to 15:10 IST
        trading_start = now_ist().replace(hour=9, minute=45, second=0, microsecond=0)
        trading_end = now_ist().replace(hour=15, minute=10, second=0, microsecond=0)
        current_time = now_ist()
        if current_time >= trading_end:
            print('Outside trading window (post 15:10). Exiting.')
            return
        if current_time < trading_start:
            wait_sec = (trading_start - current_time).total_seconds()
            print(f"Waiting until 09:45 IST to start (~{int(wait_sec)}s)...")
            while now_ist() < trading_start:
                time.sleep(5)

        while now_ist() <= trading_end:
            for sym, info in contracts.items():
                if once_per_day and bought_today.get(sym):
                    continue
                exch = info['exch']
                tsym = info['tsym']
                token = info.get('token') or info.get('tkn') or info.get('tokenno')
                if not token:
                    vals = client.search(exch, tsym)
                    if vals:
                        token = vals[0].get('token')
                if not token:
                    print(f"{sym}: missing token for {tsym}")
                    continue

                try:
                    candles = client.get_5m_candles_today(exch, token)
                except Exception as e:
                    print(f"{sym}: candle fetch error: {e}")
                    continue
                if len(candles) < 20:
                    continue

                times = [c['time'] for c in candles]
                opens = [c['open'] for c in candles]
                highs = [c['high'] for c in candles]
                lows = [c['low'] for c in candles]
                closes = [c['close'] for c in candles]

                # last fully closed bar time
                prev_bar_ts: datetime = times[-2]
                if last_bar_time.get(sym) and last_bar_time[sym] >= prev_bar_ts:
                    # still update exits if position open
                    pass

                tick = float(info.get('tick_size', 0.05))
                c1 = condition_prev_low_equals_todays_low(lows, tick)
                c2 = condition_wr_crossed_up(highs, lows, closes)
                c3 = condition_rsi_two_ago_below_20(closes)
                if c1 and c2 and c3 and sym not in open_positions:
                    if trade_segment == 'EQ':
                        prev_high = float(highs[-2])
                        prev_low = float(lows[-2])
                        computed_qty = (prev_high - prev_low) / 100.0
                        # round to nearest integer and ensure at least 1
                        qty = int(max(1, round(computed_qty)))
                    else:
                        lot = int(info.get('lot_size', 1))
                        qty = max(lot * lots_per_order, lot)
                    print(f"{sym}: BUY {tsym} qty={qty} @ {now_ist().strftime('%H:%M:%S')} (bar {prev_bar_ts.strftime('%H:%M')})")
                    try:
                        order = client.place_market_buy(exch, tsym, qty, product_type, remarks='5m-signal')
                        if order and order.get('stat') == 'Ok':
                            print(f"Order Placed: {order.get('norenordno')}")
                            # Capture entry price via LTP as proxy
                            ltp = client.get_ltp(exch, token, tsym)
                            entry_price = float(ltp) if ltp else float(closes[-1])
                            baseline_day_low = float(min(lows))
                            open_positions[sym] = {
                                'qty': qty,
                                'entry_price': entry_price,
                                'baseline_day_low': baseline_day_low,
                                'exch': exch,
                                'tsym': tsym,
                                'token': token,
                            }
                        else:
                            print(f"Order Failed: {order}")
                    except Exception as e:
                        print(f"Order Error: {e}")
                last_bar_time[sym] = prev_bar_ts

                # Exit logic for open positions: TP and SL
                if sym in open_positions:
                    pos = open_positions[sym]
                    ltp = client.get_ltp(exch, token, tsym)
                    if not ltp:
                        continue
                    entry = pos['entry_price']
                    tp_price = entry * 1.01  # +1%
                    current_day_low = float(min(lows))
                    # Take Profit
                    if ltp >= tp_price:
                        try:
                            exit_order = client.place_market_sell(exch, tsym, pos['qty'], product_type, remarks='tp-exit')
                            if exit_order and exit_order.get('stat') == 'Ok':
                                print(f"{sym}: TP hit. Sold qty={pos['qty']} at ~{ltp:.2f}")
                                bought_today[sym] = True
                                del open_positions[sym]
                                continue
                            else:
                                print(f"{sym}: TP exit failed: {exit_order}")
                        except Exception as e:
                            print(f"{sym}: TP exit error: {e}")
                    # Stoploss: new day's low after entry
                    if current_day_low < pos['baseline_day_low']:
                        try:
                            exit_order = client.place_market_sell(exch, tsym, pos['qty'], product_type, remarks='sl-exit')
                            if exit_order and exit_order.get('stat') == 'Ok':
                                print(f"{sym}: New day low. SL exit qty={pos['qty']} at ~{ltp:.2f}")
                                bought_today[sym] = True
                                del open_positions[sym]
                            else:
                                print(f"{sym}: SL exit failed: {exit_order}")
                        except Exception as e:
                            print(f"{sym}: SL exit error: {e}")

            time.sleep(poll_interval)

    finally:
        client.logout()
        print('Logged out.')


# --- Contract resolver ---

def find_nearest_month_fut(client: ShoonyaClient, underlying: str) -> Optional[Tuple[str, str, Dict]]:
    candidates = client.search('NFO', underlying + ' FUT')
    if not candidates:
        candidates = client.search('NFO', underlying)
    best = None
    best_info = None
    for c in candidates:
        exch = c.get('exch', 'NFO')
        token = c.get('token')
        tsym = c.get('tsym')
        if not token or not tsym:
            continue
        info = client.get_security_info(exch, token)
        if not info:
            continue
        inst = (info.get('instname') or '').upper()
        if 'FUT' not in inst:
            continue
        exd = info.get('exd') or info.get('exdate') or ''
        try:
            try:
                expiry = datetime.strptime(exd, '%d-%b-%Y')
            except Exception:
                expiry = datetime.strptime(exd, '%d-%m-%Y')
        except Exception:
            expiry = datetime.max
        if expiry.date() < now_ist().date():
            continue
        if best is None or expiry < best:
            best = expiry
            best_info = (exch, tsym, info)
    return best_info


if __name__ == '__main__':
    main()
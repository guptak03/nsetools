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

import pandas as pd
import pytz
from dateutil import tz
from dotenv import load_dotenv

# Try both available wrappers to improve portability
ApiClass = None
_api_import_error = None
try:
    # Newer OAuth-based package
    from NorenRestApi.NorenApi import NorenApi as ApiClass  # type: ignore
except Exception as e1:
    _api_import_error = e1
    try:
        # Legacy helper side-load pattern
        from api_helper import ShoonyaApiPy as ApiClass  # type: ignore
    except Exception as e2:
        _api_import_error = (e1, e2)
        ApiClass = None

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
                f"Shoonya API import failed. Tried NorenRestApi.NorenApi and api_helper.ShoonyaApiPy. Errors: {_api_import_error}"
            )
        self.api = ApiClass()
        self.uid: Optional[str] = None
        self.account_id: Optional[str] = None

    def login(self) -> None:
        uid = os.getenv("SHOONYA_USER")
        pwd = os.getenv("SHOONYA_PASSWORD")
        vc = os.getenv("SHOONYA_VENDOR_CODE")
        app_key = os.getenv("SHOONYA_API_KEY")
        imei = os.getenv("SHOONYA_IMEI")
        totp_secret = os.getenv("SHOONYA_TOTP_SECRET", "").strip()

        if not all([uid, pwd, vc, app_key, imei]):
            raise RuntimeError("Missing one or more required envs: SHOONYA_USER, SHOONYA_PASSWORD, SHOONYA_VENDOR_CODE, SHOONYA_API_KEY, SHOONYA_IMEI")

        twoFA = totp_now(totp_secret) if totp_secret else os.getenv("SHOONYA_OTP")
        if not twoFA:
            raise RuntimeError("Provide either SHOONYA_TOTP_SECRET or one-time SHOONYA_OTP in env")

        ret = self.api.login(userid=uid, password=pwd, twoFA=twoFA, vendor_code=vc, api_secret=app_key, imei=imei)
        if not ret or ret.get("stat") != "Ok":
            raise RuntimeError(f"Login failed: {ret}")

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

    def get_5m_candles_today(self, exch: str, token: str) -> pd.DataFrame:
        # From 09:15 IST today till now
        today = now_ist().date()
        start_dt = IST.localize(datetime(today.year, today.month, today.day, 9, 15, 0))
        end_dt = now_ist()
        # API expects epoch milliseconds or formatted string depending on wrapper; try both
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
        # Expected row format: ["YYYY-MM-DD HH:MM:SS", o, h, l, c, v]
        cols = ["time", "open", "high", "low", "close", "volume"]
        df = pd.DataFrame(rows, columns=cols[:len(rows[0])] if rows else cols)
        # Parse timestamp robustly
        def parse_ts(x: str) -> datetime:
            # Shoonya often returns IST-like local timestamps; treat as naive IST
            try:
                dt = datetime.strptime(str(x), "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    dt = datetime.strptime(str(x), "%d-%m-%Y %H:%M:%S")
                except Exception:
                    dt = datetime.fromtimestamp(int(x), tz=IST).replace(tzinfo=None)
            return IST.localize(dt)

        if not df.empty:
            df["time"] = df["time"].apply(parse_ts)
            for col in ["open", "high", "low", "close", "volume"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna().reset_index(drop=True)
        return df

    # --- Orders ---
    def place_market_buy(self, exch: str, tsym: str, qty: int, prd: str, remarks: str = "") -> Dict:
        return self.api.place_order(
            buy_or_sell='B', product_type=prd,
            exchange=exch, tradingsymbol=tsym,
            quantity=qty, discloseqty=0, price_type='MKT', price=0.0,
            retention='DAY', remarks=remarks or 'fno-bot'
        )


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = (delta.where(delta > 0, 0.0)).rolling(window=period, min_periods=period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=period, min_periods=period).mean()
    rs = gain / loss.replace(0, 1e-12)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_williams_r(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    highest_high = high.rolling(window=period, min_periods=period).max()
    lowest_low = low.rolling(window=period, min_periods=period).min()
    wr = -100 * (highest_high - close) / (highest_high - lowest_low + 1e-12)
    return wr


def find_nearest_month_fut(client: ShoonyaClient, underlying: str) -> Optional[Tuple[str, str, Dict]]:
    # Search in NFO for FUT contract tokens for the underlying
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
        # Filter only stock futures
        inst = (info.get('instname') or '').upper()
        if 'FUT' not in inst:
            continue
        # Parse expiry date 'exd' in DD-MMM-YYYY or DD-MM-YYYY
        exd = info.get('exd') or info.get('exdate') or ''
        try:
            try:
                expiry = datetime.strptime(exd, '%d-%b-%Y')
            except Exception:
                expiry = datetime.strptime(exd, '%d-%m-%Y')
        except Exception:
            # fallback: very large date
            expiry = datetime.max
        # choose nearest future expiry in future (>= today)
        if expiry.date() < now_ist().date():
            continue
        if best is None or expiry < best:
            best = expiry
            best_info = (exch, tsym, info)
    return best_info


def condition_prev_low_equals_todays_low(df: pd.DataFrame, tick_size: float) -> bool:
    if df.shape[0] < 20:
        return False
    # Use last fully closed bar as prev (index -2)
    prev_low = float(df['low'].iloc[-2])
    today_low = float(df['low'].min())
    return abs(prev_low - today_low) <= max(tick_size, 1e-6)


def condition_wr_crossed_up(df: pd.DataFrame) -> bool:
    wr = compute_williams_r(df['high'], df['low'], df['close'], period=14)
    if wr.shape[0] < 3 or pd.isna(wr.iloc[-2]) or pd.isna(wr.iloc[-3]):
        return False
    return (wr.iloc[-3] < -80) and (wr.iloc[-2] > -80)


def condition_rsi_two_ago_below_20(df: pd.DataFrame) -> bool:
    rsi = compute_rsi(df['close'], period=14)
    if rsi.shape[0] < 3 or pd.isna(rsi.iloc[-3]):
        return False
    return rsi.iloc[-3] < 20


def main() -> None:
    load_dotenv()

    watchlist = [s.strip().upper() for s in os.getenv('WATCHLIST', '').split(',') if s.strip()]
    if not watchlist:
        print('WATCHLIST env is empty. Example: WATCHLIST=RELIANCE, HDFCBANK, TCS')
        sys.exit(1)

    product_type = os.getenv('PRODUCT_TYPE', 'I').upper()
    lots_per_order = int(os.getenv('LOTS_PER_ORDER', '1'))
    once_per_day = os.getenv('ONCE_PER_DAY', 'true').lower() == 'true'
    poll_interval = int(os.getenv('POLL_INTERVAL_SEC', '20'))

    client = ShoonyaClient()
    client.login()

    try:
        # Resolve contracts
        contracts: Dict[str, Dict] = {}
        for sym in watchlist:
            res = find_nearest_month_fut(client, sym)
            if not res:
                print(f"No FUT contract found for {sym}")
                continue
            exch, tsym, info = res
            info['exch'] = exch
            info['tsym'] = tsym
            # Lot size and tick size fallbacks
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

        market_close = now_ist().replace(hour=15, minute=30, second=0, microsecond=0)
        if now_ist() > market_close:
            print('Market closed. Exiting.')
            return

        while now_ist() < market_close:
            for sym, info in contracts.items():
                if once_per_day and bought_today.get(sym):
                    continue
                exch = info['exch']
                tsym = info['tsym']
                token = info.get('token') or info.get('tkn') or info.get('tokenno')
                # If token not retained from info, reacquire via search
                if not token:
                    vals = client.search(exch, tsym)
                    if vals:
                        token = vals[0].get('token')
                if not token:
                    print(f"{sym}: missing token for {tsym}")
                    continue

                try:
                    df = client.get_5m_candles_today(exch, token)
                except Exception as e:
                    print(f"{sym}: candle fetch error: {e}")
                    continue
                if df.empty or df.shape[0] < 20:
                    continue

                # Ensure we act once per new bar
                prev_bar_ts: datetime = df['time'].iloc[-2].to_pydatetime()
                if last_bar_time.get(sym) and last_bar_time[sym] >= prev_bar_ts:
                    continue

                # Evaluate conditions
                tick = float(info.get('tick_size', 0.05))
                c1 = condition_prev_low_equals_todays_low(df, tick)
                c2 = condition_wr_crossed_up(df)
                c3 = condition_rsi_two_ago_below_20(df)
                if c1 and c2 and c3:
                    lot = int(info.get('lot_size', 1))
                    qty = max(lot * lots_per_order, lot)
                    print(f"{sym}: BUY {tsym} qty={qty} @ {now_ist().strftime('%H:%M:%S')} (bar {prev_bar_ts.strftime('%H:%M')})")
                    try:
                        order = client.place_market_buy(exch, tsym, qty, product_type, remarks='5m-signal')
                        if order and order.get('stat') == 'Ok':
                            print(f"Order Placed: {order.get('norenordno')}")
                            bought_today[sym] = True
                        else:
                            print(f"Order Failed: {order}")
                    except Exception as e:
                        print(f"Order Error: {e}")
                last_bar_time[sym] = prev_bar_ts

            time.sleep(poll_interval)

    finally:
        client.logout()
        print('Logged out.')


if __name__ == '__main__':
    main()
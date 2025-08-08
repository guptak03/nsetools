import os
import json
import time
import hashlib
import requests
from typing import Any, Dict, Optional


class ShoonyaApiPy:
    def __init__(self, host: Optional[str] = None) -> None:
        self.host = host or os.getenv('SHOONYA_HOST', 'https://api.shoonya.com/NorenWClient/')
        if not self.host.endswith('/'):
            self.host += '/'
        self.susertoken: Optional[str] = None
        self.uid: Optional[str] = None
        self.session = requests.Session()
        self.common_headers = {
            'User-Agent': 'shoonya-bot/1.0',
            'Accept': 'application/json, text/plain, */*',
        }

    def _post(self, route: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self.host + route
        headers = self.common_headers.copy()
        if self.susertoken:
            headers['jKey'] = self.susertoken
        # Shoonya expects form-encoded with jData=JSON
        data = {'jData': json.dumps(payload)}
        resp = self.session.post(url, data=data, headers=headers, timeout=15)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {'stat': 'Not_Ok', 'emsg': f'Invalid JSON from {route}', 'raw': resp.text}

    # --- Public API methods expected by bot.py ---
    def login(self, userid: str, password: str, twoFA: str, vendor_code: Optional[str] = None,
              api_secret: Optional[str] = None, imei: Optional[str] = None) -> Dict[str, Any]:
        # API sometimes expects SHA256(password). Try plain first, then SHA256 fallback
        def attempt(pwd_value: str) -> Dict[str, Any]:
            payload = {
                'apkversion': '1.0.0',
                'uid': userid,
                'pwd': pwd_value,
                'factor2': twoFA,
                'vc': vendor_code or '',
                'appkey': api_secret or '',
                'imei': imei or 'abc',
                'source': 'API',
            }
            return self._post('QuickAuth', payload)

        ret = attempt(password)
        if not ret or ret.get('stat') != 'Ok':
            sha = hashlib.sha256(password.encode()).hexdigest()
            ret = attempt(sha)
        if ret and ret.get('stat') == 'Ok':
            self.susertoken = ret.get('susertoken')
            self.uid = userid
        return ret

    def logout(self) -> Dict[str, Any]:
        if not self.uid:
            return {'stat': 'Ok'}
        return self._post('Logout', {'uid': self.uid})

    def searchscrip(self, exchange: str, searchtext: str) -> Dict[str, Any]:
        return self._post('SearchScrip', {'exch': exchange, 'stext': searchtext, 'uid': self.uid or ''})

    def get_security_info(self, exchange: str, token: str) -> Dict[str, Any]:
        return self._post('GetSecurityInfo', {'exch': exchange, 'token': token, 'uid': self.uid or ''})

    def get_time_price_series(self, exchange: str, token: str, starttime: Any, endtime: Any, interval: int = 5) -> Dict[str, Any]:
        # Shoonya expects epoch seconds
        try:
            st = int(starttime)
            et = int(endtime)
        except Exception:
            st = int(time.time()) - 86400
            et = int(time.time())
        return self._post('TPSeries', {
            'exch': exchange,
            'token': token,
            'st': st,
            'et': et,
            'intrv': interval,
            'uid': self.uid or '',
        })

    def get_quotes(self, exchange: str, token: str) -> Dict[str, Any]:
        return self._post('GetQuotes', {'exch': exchange, 'token': token, 'uid': self.uid or ''})

    def place_order(self, buy_or_sell: str, product_type: str, exchange: str, tradingsymbol: str,
                    quantity: int, discloseqty: int, price_type: str, price: float = 0.0,
                    trigger_price: Optional[float] = None, retention: str = 'DAY', amo: str = 'NO',
                    remarks: Optional[str] = None) -> Dict[str, Any]:
        payload = {
            'uid': self.uid or '',
            'actid': self.uid or '',
            'exch': exchange,
            'tsym': tradingsymbol,
            'qty': quantity,
            'prc': price,
            'dscqty': discloseqty,
            'prd': product_type,
            'trantype': buy_or_sell,
            'prctyp': price_type,
            'ret': retention,
            'remarks': remarks or '',
            'amo': 'Yes' if amo.upper() == 'YES' else 'NO',
        }
        if trigger_price is not None:
            payload['trgprc'] = trigger_price
        return self._post('PlaceOrder', payload)
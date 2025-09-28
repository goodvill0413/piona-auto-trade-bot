import os
import json
import time
import hmac
import base64
import hashlib
import logging
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# .env 로드
load_dotenv()

# SSL 경고 무시(OKX 호출 시 편의)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 로깅
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ==============================
# 유틸: 수량 정규화
# ==============================
def normalize_size(amount, lot_size, min_size):
    """OKX 주문 수량을 lotSz 배수이면서 minSz 이상으로 정규화"""
    amt = Decimal(str(amount))
    lot = Decimal(str(lot_size))
    minz = Decimal(str(min_size))
    if amt < minz:
        amt = minz
    multiples = (amt / lot).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    if multiples <= 0:
        multiples = Decimal('1')
    normalized = (multiples * lot)
    return float(normalized)


# ==============================
# OKX Trader
# ==============================
class OKXTrader:
    def __init__(self):
        self.api_key     = os.getenv('OKX_API_KEY')
        self.secret_key  = os.getenv('OKX_API_SECRET')
        self.passphrase  = os.getenv('OKX_API_PASSPHRASE')
        self.base_url    = os.getenv('OKX_BASE_URL', 'https://www.okx.com')
        self.simulated   = os.getenv('OKX_SIMULATED', '1')              # '1'이면 x-simulated-trading 헤더 전송
        self.default_tdmode  = os.getenv('DEFAULT_TDMODE', 'cross')     # cross | isolated | cash
        self.default_market  = os.getenv('DEFAULT_MARKET', 'swap')      # swap | spot
        logger.info(f"OKXTrader 초기화 - simulated={self.simulated}, market={self.default_market}, tdMode={self.default_tdmode}")

        # 공통 헤더(공개 API용)
        self.public_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json,text/plain,*/*"
        }

    # --- 공통: OKX 타임스탬프(UTC, ms) ---
    def get_timestamp(self):
        try:
            r = requests.get(self.base_url + "/api/v5/public/time", timeout=3, verify=False, headers=self.public_headers)
            ts_ms = int(r.json()["data"][0]["ts"])
            return datetime.utcfromtimestamp(ts_ms / 1000).isoformat(timespec="milliseconds") + "Z"
        except Exception:
            return datetime.utcnow().isoformat(timespec="milliseconds") + "Z"

    # --- 공통: 요청 서명 ---
    def sign_request(self, method, path, body=""):
        if not self.secret_key or not self.api_key or not self.passphrase:
            raise RuntimeError("OKX API 인증정보(키/시크릿/패스프레이즈)가 설정되지 않았습니다.")
        timestamp = self.get_timestamp()
        message   = timestamp + method + path + (body or "")
        signature = base64.b64encode(hmac.new(self.secret_key.encode(), message.encode(), hashlib.sha256).digest()).decode()
        headers = {
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-SIGN': signature,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': self.passphrase,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        if self.simulated == '1':
            headers['x-simulated-trading'] = '1'
        return headers

    # --- 간단 재시도 래퍼(공개 API용) ---
    def _get_with_retry(self, url, params=None, headers=None, tries=3, backoff=0.6):
        last = None
        for i in range(tries):
            try:
                r = requests.get(url, params=params, headers=headers or self.public_headers, verify=False, timeout=10)
                # 디버그 로그
                logger.error(f"[HTTP GET] try={i+1}/{tries} url={r.url} status={r.status_code} head={dict(r.headers)} text={r.text[:300]!r}")
                if r.status_code == 200 and r.text:
                    return r
            except Exception as e:
                logger.error(f"[HTTP GET] 예외 try={i+1}: {e}")
            time.sleep(backoff * (i + 1))
            last = r if 'r' in locals() else None
        return last

    # --- 계정 설정 조회(posMode 확인용) ---
    def get_account_config(self):
        method = "GET"
        path   = "/api/v5/account/config"
        headers = self.sign_request(method, path)
        try:
            r = requests.get(self.base_url + path, headers=headers, verify=False, timeout=10)
            logger.info(f"[ACCOUNT CONFIG] status={r.status_code} text={r.text[:200]!r}")
            data = r.json()
            if data.get("code") == "0" and data.get("data"):
                return data["data"][0]
            logger.error(f"계정 설정 조회 실패: {data}")
            return None
        except Exception as e:
            logger.error(f"계정 설정 조회 오류: {e}")
            return None

    # --- 종목 메타 조회(minSz/lotSz 등) : 헤더/재시도/RAW 로그 강화 ---
    def get_instrument_info(self, symbol):
        """
        OKX 종목 메타(minSz, lotSz 등) 조회.
        - UA/Accept 헤더로 CDN 차단 방지
        - 재시도+백오프
        - instId 직접조회 실패 시 목록조회 폴백
        - RAW 응답 스니펫 로그
        """
        path = "/api/v5/public/instruments"
        # 1) instId 직접 조회
        r = self._get_with_retry(self.base_url + path,
                                 params={"instType": "SWAP", "instId": symbol})
        try:
            if r and r.status_code == 200:
                data = r.json()
                if data.get("code") == "0" and data.get("data"):
                    return data["data"][0]
                else:
                    logger.error(f"[INSTRUMENT:direct] JSON code/data 이상: {data}")
            else:
                logger.error(f"[INSTRUMENT:direct] HTTP 비정상 or 응답없음: {None if r is None else r.status_code}")
        except Exception as e:
            logger.error(f"[INSTRUMENT:direct] JSON 파싱 실패: {e}")

        # 2) 목록 조회(폴백)
        r2 = self._get_with_retry(self.base_url + path,
                                  params={"instType": "SWAP"})
        try:
            if r2 and r2.status_code == 200:
                data2 = r2.json()
                if data2.get("code") == "0" and data2.get("data"):
                    for item in data2["data"]:
                        if item.get("instId") == symbol:
                            return item
                else:
                    logger.error(f"[INSTRUMENT:list] JSON code/data 이상: {data2}")
            else:
                logger.error(f"[INSTRUMENT:list] HTTP 비정상 or 응답없음: {None if r2 is None else r2.status_code}")
        except Exception as e:
            logger.error(f"[INSTRUMENT:list] JSON 파싱 실패: {e}")

        return None

    # --- 주문 실행 ---
    def place_order(self, symbol, side, amount, price=None, order_type="market", td_mode=None):
        inst = self.get_instrument_info(symbol)
        if not inst:
            logger.error(f"주문 실패: {symbol} 심볼 정보 조회 실패")
            return {"code": "error", "msg": "심볼 정보 조회 실패"}

        lot_size = float(inst.get("lotSz", 0.001))
        min_size = float(inst.get("minSz", 0.001))
        amount   = normalize_size(amount, lot_size, min_size)

        acc_cfg  = self.get_account_config()
        pos_mode = (acc_cfg.get("posMode") if acc_cfg else "net_mode")  # net_mode | long_short_mode

        method = "POST"
        path   = "/api/v5/trade/order"

        if td_mode is None:
            td_mode = "cash" if self.default_market == "spot" else self.default_tdmode

        body = {
            "instId": symbol,
            "tdMode": td_mode,
            "side":   side,
            "ordType": order_type,
            "sz":     str(amount)
        }
        if pos_mode == "long_short_mode":
            body["posSide"] = "long" if side == "buy" else "short"
        if price and order_type == "limit":
            body["px"] = str(price)

        body_str = json.dumps(body)
        headers  = self.sign_request(method, path, body_str)
        logger.info(f"주문 요청: instId={symbol}, side={side}, sz={amount}, ordType={order_type}, tdMode={td_mode}, posMode={pos_mode}, body={body}")

        try:
            response = requests.post(self.base_url + path, headers=headers, data=body_str, verify=False, timeout=10)
            logger.info(f"주문 응답(raw): {response.text}")
            result = response.json()
            return result
        except Exception as e:
            logger.error(f"주문 실행 오류: {e}")
            return {"code": "error", "msg": str(e)}

    # --- 포지션 종료(간단형) ---
    def close_position(self, symbol, side="both"):
        """
        side: 'long' | 'short' | 'both'
        간단화를 위해 시장가 반대 주문으로 처리
        """
        acc_cfg  = self.get_account_config()
        pos_mode = (acc_cfg.get("posMode") if acc_cfg else "net_mode")

        inst = self.get_instrument_info(symbol)
        if not inst:
            return {"code": "error", "msg": "심볼 정보 조회 실패"}

        lot_size = float(inst.get("lotSz", 0.001))
        min_size = float(inst.get("minSz", 0.001))
        sz_min   = normalize_size(min_size, lot_size, min_size)

        results = []
        sides_to_close = []
        if side == "both" and pos_mode == "long_short_mode":
            sides_to_close = ["sell", "buy"]  # long 청산은 sell, short 청산은 buy
        else:
            sides_to_close = ["sell" if side in ["both", "long"] else "buy"]

        for s in sides_to_close:
            res = self.place_order(symbol, s, sz_min, order_type="market")
            results.append(res)
        return {"code": "0", "results": results}


# ==============================
# Webhook 유틸
# ==============================
def validate_webhook_token(token):
    expected_token = os.getenv('WEBHOOK_TOKEN', 'test123')
    return token == expected_token and token != 'change-me'


def parse_tradingview_webhook(data, trader: OKXTrader):
    """TradingView 웹훅 파싱 + 심볼 보정(-SWAP)"""
    try:
        webhook_data = data if isinstance(data, dict) else json.loads(data)
        for field in ['action', 'symbol']:
            if field not in webhook_data:
                raise ValueError(f"필수 필드 누락: {field}")

        sym = webhook_data['symbol']
        if isinstance(sym, str) and sym and sym.upper() != 'NONE':
            # swap 기본이면 '-SWAP' 자동 부착 (이미 붙어있으면 그대로)
            if trader.default_market == "swap" and not sym.endswith("-SWAP"):
                sym = sym + "-SWAP"

        quantity = float(webhook_data.get('quantity', 1.0))
        return {
            'action': webhook_data['action'].lower(),
            'symbol': sym,
            'quantity': quantity,
            'price': webhook_data.get('price'),
            'order_type': webhook_data.get('order_type', 'market'),
            'message': webhook_data.get('message', ''),
            'token': webhook_data.get('token', '')
        }
    except Exception as e:
        logger.error(f"웹훅 파싱 오류: {e}")
        return None


# ==============================
# Flask Routes
# ==============================
trader = OKXTrader()

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        webhook_data = request.get_json() if request.is_json else request.get_data(as_text=True)
        logger.info(f"웹훅 수신: {webhook_data}")
        parsed = parse_tradingview_webhook(webhook_data, trader)
        if not parsed:
            return jsonify({"status": "error", "message": "유효하지 않은 웹훅 데이터"}), 400
        if not validate_webhook_token(parsed['token']):
            logger.warning("유효하지 않은 토큰으로 웹훅 호출")
            return jsonify({"status": "error", "message": "유효하지 않은 토큰"}), 403

        action     = parsed['action']
        symbol     = parsed['symbol']
        quantity   = float(parsed['quantity'])
        price      = parsed.get('price')
        order_type = parsed['order_type']

        if action in ['buy', 'sell']:
            result = trader.place_order(symbol=symbol, side=action, amount=quantity, price=price, order_type=order_type)
        elif action == 'close':
            result = trader.close_position(symbol, 'both')
        else:
            return jsonify({"status": "error", "message": f"지원하지 않는 액션: {action}"}), 400

        if result.get('code') == '0':
            return jsonify({"status": "success", "message": f"{action} 주문 실행 완료", "data": result})
        else:
            return jsonify({"status": "error", "message": f"주문 실패: {result.get('msg', '알 수 없는 오류')}", "data": result}), 500
    except Exception as e:
        logger.error(f"웹훅 처리 오류: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        "status": "running",
        "timestamp": datetime.now().isoformat(),
        "market": trader.default_market,
        "simulated": (trader.simulated == '1')
    })


# 로컬 개발용
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8000)


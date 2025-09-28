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
import os
import json
import time
import hmac
import base64
import hashlib
import logging
import requests
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from flask import Flask, request, jsonify
from dotenv import load_dotenv

# .env 로드
load_dotenv()

# SSL 경고 숨기기
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
    """
    OKX 주문 수량을 lotSz 배수이면서 minSz 이상이 되도록 정규화.
    """
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


class OKXTrader:
    def __init__(self):
        self.api_key = os.getenv('OKX_API_KEY')
        self.secret_key = os.getenv('OKX_API_SECRET')
        self.passphrase = os.getenv('OKX_API_PASSPHRASE')
        self.base_url = os.getenv('OKX_BASE_URL', 'https://www.okx.com')
        self.simulated = os.getenv('OKX_SIMULATED', '1')      # '1' 이면 x-simulated-trading 헤더 전송
        self.default_tdmode = os.getenv('DEFAULT_TDMODE', 'cross')  # cross | isolated | cash
        self.default_market = os.getenv('DEFAULT_MARKET', 'swap')   # swap | spot
        logger.info(f"OKXTrader 초기화 - 시뮬레이션 모드: {self.simulated}, 마켓: {self.default_market}, tdMode(default): {self.default_tdmode}")

    def get_timestamp(self):
        """OKX 서버 시간을 우선 사용하여 ISO8601 UTC(ms) 타임스탬프 생성."""
        try:
            r = requests.get(self.base_url + "/api/v5/public/time", timeout=3, verify=False)
            ts_ms = int(r.json()["data"][0]["ts"])  # milliseconds
            return datetime.utcfromtimestamp(ts_ms / 1000).isoformat(timespec="milliseconds") + "Z"
        except Exception:
            return datetime.utcnow().isoformat(timespec="milliseconds") + "Z"

    def sign_request(self, method, path, body=""):
        timestamp = self.get_timestamp()
        message = timestamp + method + path + (body or "")
        signature = base64.b64encode(
            hmac.new(self.secret_key.encode(), message.encode(), hashlib.sha256).digest()
        ).decode()
        headers = {
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-SIGN': signature,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': self.passphrase,
            'Content-Type': 'application/json'
        }
        if self.simulated == '1':
            headers['x-simulated-trading'] = '1'
        return headers

    def get_account_config(self):
        """계정 설정(포지션 모드 등) 조회: posMode = net_mode | long_short_mode"""
        method = "GET"
        path = "/api/v5/account/config"
        headers = self.sign_request(method, path)
        try:
            r = requests.get(self.base_url + path, headers=headers, verify=False, timeout=10)
            data = r.json()
            if data.get("code") == "0" and data.get("data"):
                return data["data"][0]
            logger.error(f"계정 설정 조회 실패: {data}")
            return None
        except Exception as e:
            logger.error(f"계정 설정 조회 오류: {e}")
            return None

    def get_instrument_info(self, symbol):
        """주문 규칙(minSz, lotSz 등) 조회"""
        try:
            response = requests.get(
                f"{self.base_url}/api/v5/public/instruments?instType=SWAP&instId={symbol}",
                verify=False,
                timeout=5
            )
            data = response.json()
            if data.get('code') == '0' and data.get('data'):
                logger.info(f"심볼 정보 조회 성공: {symbol}, minSz={data['data'][0]['minSz']}, lotSz={data['data'][0]['lotSz']}")
                return data['data'][0]
            logger.error(f"심볼 정보 조회 실패: {data}")
            return None
        except Exception as e:
            logger.error(f"심볼 정보 조회 오류: {e}")
            return None

    def get_ticker(self, symbol):
        """현재가 조회"""
        try:
            response = requests.get(
                f"{self.base_url}/api/v5/market/ticker?instId={symbol}",
                verify=False,
                timeout=5
            )
            data = response.json()
            if data.get('code') == '0' and data.get('data'):
                return float(data['data'][0]['last'])
            logger.error(f"가격 조회 실패: {data}")
            return None
        except Exception as e:
            logger.error(f"가격 조회 오류: {e}")
            return None

    def get_positions(self, symbol=None):
        """포지션 조회"""
        method = "GET"
        path = "/api/v5/account/positions"
        if symbol:
            path += f"?instId={symbol}"
        headers = self.sign_request(method, path)
        try:
            response = requests.get(self.base_url + path, headers=headers, verify=False, timeout=10)
            return response.json()
        except Exception as e:
            logger.error(f"포지션 조회 오류: {e}")
            return {"code": "error", "msg": str(e)}

    def close_position(self, symbol, side):
        """포지션 청산 (net/hedge 공통)"""
        positions = self.get_positions(symbol)
        logger.info(f"포지션 조회 결과: {positions}")
        if positions.get('code') != '0':
            return positions
        for pos in positions.get('data', []):
            if pos.get('instId') == symbol and float(pos.get('pos', 0)) != 0:
                pos_side = pos['posSide']
                pos_size = abs(float(pos['pos']))
                close_side = "sell" if pos_side == "long" else "buy"
                return self.place_order(
                    symbol=symbol,
                    side=close_side,
                    amount=pos_size,
                    order_type="market",
                    td_mode=pos.get('mgnMode', self.default_tdmode)
                )
        return {"code": "0", "msg": "청산할 포지션이 없습니다"}

    def place_order(self, symbol, side, amount, price=None, order_type="market", td_mode=None):
        """주문 실행"""
        # 심볼 정보
        instrument_info = self.get_instrument_info(symbol)
        if not instrument_info:
            logger.error(f"주문 실패: {symbol} 심볼 정보 조회 실패")
            return {"code": "error", "msg": "심볼 정보 조회 실패"}

        lot_size = float(instrument_info['lotSz'])
        min_size = float(instrument_info['minSz'])

        # 수량 정규화
        amount = normalize_size(amount, lot_size, min_size)

        # 포지션 모드 (net_mode / long_short_mode)
        acc_cfg = self.get_account_config()
        pos_mode = (acc_cfg.get("posMode") if acc_cfg else "net_mode")

        method = "POST"
        path = "/api/v5/trade/order"

        # tdMode 결정: 명시 없으면 env 기본, spot이면 cash
        if td_mode is None:
            td_mode = "cash" if self.default_market == "spot" else self.default_tdmode
        # (중요) 더 이상 simulated+swap에서 cross 강제하지 않음 — env/요청값을 그대로 사용

        body = {
            "instId": symbol,
            "tdMode": td_mode,
            "side": side,
            "ordType": order_type,
            "sz": str(amount)
        }

        # hedge 모드면 posSide 필요 / net 모드면 생략
        if pos_mode == "long_short_mode":
            body["posSide"] = "long" if side == "buy" else "short"

        if price and order_type == "limit":
            body["px"] = str(price)

        body_str = json.dumps(body)
        headers = self.sign_request(method, path, body_str)
        logger.info(f"주문 시도: instId={symbol}, side={side}, sz={amount}, ordType={order_type}, tdMode={td_mode}, posMode={pos_mode}, body={body}")
        try:
            response = requests.post(self.base_url + path, headers=headers, data=body_str, verify=False, timeout=10)
            result = response.json()
            logger.info(f"주문 응답: {response.text}")
            return result
        except Exception as e:
            logger.error(f"주문 실행 오류: {e}")
            return {"code": "error", "msg": str(e)}


def validate_webhook_token(token):
    """웹훅 토큰 검증"""
    expected_token = os.getenv('WEBHOOK_TOKEN', 'test123')
    return token == expected_token and token != 'change-me'


def parse_tradingview_webhook(data):
    """TradingView 웹훅 데이터 파싱 + 심볼/수량 정규화"""
    try:
        webhook_data = data if isinstance(data, dict) else json.loads(data)

        required_fields = ['action', 'symbol']
        for field in required_fields:
            if field not in webhook_data:
                raise ValueError(f"필수 필드 누락: {field}")

        # 심볼 정규화: swap 기본이면 '-SWAP' 보장
        sym = webhook_data['symbol']
        if isinstance(sym, str) and sym and sym.upper() != 'NONE':
            if trader.default_market == "swap" and not sym.endswith("-SWAP"):
                sym = sym + "-SWAP"

        # 수량 정규화
        quantity = float(webhook_data.get('quantity', 1))
        instrument_info = trader.get_instrument_info(sym)
        if instrument_info:
            lot_size = float(instrument_info['lotSz'])
            min_size = float(instrument_info['minSz'])
            quantity = normalize_size(quantity, lot_size, min_size)

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
        logger.error(f"웹훅 데이터 파싱 오류: {e}")
        return None


# 전역 트레이더
trader = OKXTrader()

# ========= Routes =========
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        webhook_data = request.get_json() if request.is_json else request.get_data(as_text=True)
        logger.info(f"웹훅 수신: {webhook_data}")
        parsed = parse_tradingview_webhook(webhook_data)
        if not parsed:
            return jsonify({"status": "error", "message": "잘못된 웹훅 데이터"}), 400
        if not validate_webhook_token(parsed['token']):
            logger.warning("유효하지 않은 토큰으로 웹훅 요청")
            return jsonify({"status": "error", "message": "유효하지 않은 토큰"}), 403

        action = parsed['action']
        symbol = parsed['symbol']
        quantity = float(parsed['quantity'])
        price = parsed.get('price')
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
        "simulated": trader.simulated == '1'
    })


@app.route('/positions', methods=['GET'])
def get_positions():
    try:
        positions = trader.get_positions()
        return jsonify(positions)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/balance', methods=['GET'])
def get_balance():
    try:
        method = "GET"
        path = "/api/v5/account/balance"
        headers = trader.sign_request(method, path)
        response = requests.get(trader.base_url + path, headers=headers, verify=False, timeout=10)
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/account_config', methods=['GET'])
def get_account_config_route():
    try:
        config = trader.get_account_config()
        if config:
            return jsonify({"status": "success", "data": config})
        else:
            return jsonify({"status": "error", "message": "계정 설정 조회 실패"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/set_leverage', methods=['POST'])
def set_leverage():
    """레버리지/증거금 모드 설정 (cross/isolated)"""
    try:
        data = request.get_json()
        inst_id = data.get('instId', 'BTC-USDT-SWAP')
        lever = data.get('lever', '10')
        mgn_mode = data.get('mgnMode', 'cross')

        method = "POST"
        path = "/api/v5/account/set-leverage"

        body = {"instId": inst_id, "lever": lever, "mgnMode": mgn_mode}
        body_str = json.dumps(body)
        headers = trader.sign_request(method, path, body_str)

        response = requests.post(trader.base_url + path, headers=headers, data=body_str, verify=False, timeout=10)
        result = response.json()

        if result.get('code') == '0':
            return jsonify({"status": "success", "message": f"{inst_id} 레버리지 {lever}x 설정 완료", "data": result})
        else:
            return jsonify({"status": "error", "message": f"레버리지 설정 실패: {result.get('msg', '알 수 없는 오류')}", "data": result}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/position_mode', methods=['GET', 'POST'])
def position_mode():
    """포지션 모드 조회/변경"""
    try:
        if request.method == 'GET':
            config = trader.get_account_config()
            if config:
                return jsonify({"status": "success", "posMode": config.get('posMode', 'net_mode'), "data": config})
            else:
                return jsonify({"status": "error", "message": "포지션 모드 조회 실패"}), 500

        data = request.get_json()
        pos_mode = data.get('posMode', 'net_mode')  # net_mode | long_short_mode

        method = "POST"
        path = "/api/v5/account/set-position-mode"

        body = {"posMode": pos_mode}
        body_str = json.dumps(body)
        headers = trader.sign_request(method, path, body_str)

        response = requests.post(trader.base_url + path, headers=headers, data=body_str, verify=False, timeout=10)
        result = response.json()

        if result.get('code') == '0':
            return jsonify({"status": "success", "message": f"포지션 모드 {pos_mode} 설정 완료", "data": result})
        else:
            return jsonify({"status": "error", "message": f"포지션 모드 설정 실패: {result.get('msg', '알 수 없는 오류')}", "data": result}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    print("=== TradingView → OKX 자동매매 시스템 시작 ===")
    print(f"시뮬레이션 모드: {trader.simulated == '1'}")
    print(f"기본 마켓: {trader.default_market}")
    print(f"기본 거래 모드(tdMode): {trader.default_tdmode}")
    print("웹훅 URL: http://localhost:5000/webhook")
    print("상태 확인: http://localhost:5000/status")
    print("=" * 50)

    info = trader.get_instrument_info("BTC-USDT-SWAP")
    if info:
        print(f"최소 주문 수량(minSz): {info['minSz']}")
        print(f"단위(Lot Size): {info['lotSz']}")

    app.run(host='0.0.0.0', port=5000, debug=True)

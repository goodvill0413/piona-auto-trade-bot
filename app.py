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

# SSL 경고 무시
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ==============================
# 유틸리티 함수
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


def validate_webhook_token(token):
    """웹훅 토큰 검증"""
    expected_token = os.getenv('WEBHOOK_TOKEN', 'test123')
    return token == expected_token and token != 'change-me'


def parse_tradingview_webhook(data, trader):
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
# OKX Trader 클래스
# ==============================
class OKXTrader:
    def __init__(self):
        self.api_key = os.getenv('OKX_API_KEY')
        self.secret_key = os.getenv('OKX_API_SECRET')
        self.passphrase = os.getenv('OKX_API_PASSPHRASE')
        self.base_url = os.getenv('OKX_BASE_URL', 'https://www.okx.com')
        self.simulated = os.getenv('OKX_SIMULATED', '1')
        self.default_tdmode = os.getenv('DEFAULT_TDMODE', 'cross')
        self.default_market = os.getenv('DEFAULT_MARKET', 'swap')
        
        logger.info(f"OKXTrader 초기화 - simulated={self.simulated}, market={self.default_market}, tdMode={self.default_tdmode}")
        
        # 공통 헤더 (봇 차단 방지)
        self.public_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        }

    def get_timestamp(self):
        """OKX 서버 시간 기반 ISO8601 UTC 타임스탬프"""
        try:
            r = requests.get(self.base_url + "/api/v5/public/time", 
                           timeout=10, verify=False, headers=self.public_headers)
            logger.info(f"[TIMESTAMP] status={r.status_code} response={r.text[:100]}")
            if r.status_code == 200 and r.text.strip():
                data = r.json()
                ts_ms = int(data["data"][0]["ts"])
                return datetime.utcfromtimestamp(ts_ms / 1000).isoformat(timespec="milliseconds") + "Z"
        except Exception as e:
            logger.error(f"[TIMESTAMP] 오류: {e}")
        
        return datetime.utcnow().isoformat(timespec="milliseconds") + "Z"

    def sign_request(self, method, path, body=""):
        """요청 서명 생성"""
        if not all([self.secret_key, self.api_key, self.passphrase]):
            raise RuntimeError("OKX API 인증정보가 설정되지 않았습니다.")
        
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
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        if self.simulated == '1':
            headers['x-simulated-trading'] = '1'
        
        return headers

    def _safe_request(self, url, params=None, headers=None, timeout=10):
        """안전한 HTTP 요청 (디버그 강화)"""
        try:
            logger.info(f"[REQUEST] url={url} params={params}")
            r = requests.get(url, params=params, 
                           headers=headers or self.public_headers, 
                           verify=False, timeout=timeout)
            
            logger.info(f"[RESPONSE] status={r.status_code} content_length={len(r.text)} headers={dict(r.headers)}")
            logger.info(f"[RESPONSE_TEXT] {r.text[:500]}")
            
            if r.status_code == 200 and r.text.strip():
                try:
                    data = r.json()
                    logger.info(f"[JSON_SUCCESS] code={data.get('code')} data_count={len(data.get('data', []))}")
                    return data
                except json.JSONDecodeError as e:
                    logger.error(f"[JSON_ERROR] {e} - Raw response: {r.text[:200]}")
                    return None
            else:
                logger.error(f"[HTTP_ERROR] status={r.status_code} text={r.text[:200]}")
                return None
                
        except Exception as e:
            logger.error(f"[REQUEST_ERROR] {e}")
            return None

    def get_account_config(self):
        """계정 설정 조회 (디버그 강화)"""
        method = "GET"
        path = "/api/v5/account/config"
        headers = self.sign_request(method, path)
        
        try:
            logger.info(f"[ACCOUNT_CONFIG] 요청 시작")
            r = requests.get(self.base_url + path, headers=headers, verify=False, timeout=10)
            logger.info(f"[ACCOUNT_CONFIG] status={r.status_code} response={r.text[:200]}")
            
            if r.status_code == 200 and r.text.strip():
                data = r.json()
                if data.get("code") == "0" and data.get("data"):
                    logger.info(f"[ACCOUNT_CONFIG] 성공: {data['data'][0]}")
                    return data["data"][0]
                else:
                    logger.error(f"[ACCOUNT_CONFIG] API 오류: {data}")
                    return None
            else:
                logger.error(f"[ACCOUNT_CONFIG] HTTP 오류: status={r.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"[ACCOUNT_CONFIG] 예외: {e}")
            return None

    def get_instrument_info(self, symbol):
        """종목 정보 조회 (디버그 강화)"""
        logger.info(f"[INSTRUMENT] 심볼 조회 시작: {symbol}")
        
        # 1차: 직접 조회
        url = f"{self.base_url}/api/v5/public/instruments"
        params = {"instType": "SWAP", "instId": symbol}
        
        data = self._safe_request(url, params)
        if data and data.get("code") == "0" and data.get("data"):
            logger.info(f"[INSTRUMENT] 직접 조회 성공: {symbol}")
            return data["data"][0]
        
        # 2차: 전체 목록에서 검색
        logger.info(f"[INSTRUMENT] 전체 목록 조회로 폴백")
        params2 = {"instType": "SWAP"}
        data2 = self._safe_request(url, params2)
        
        if data2 and data2.get("code") == "0" and data2.get("data"):
            for item in data2["data"]:
                if item.get("instId") == symbol:
                    logger.info(f"[INSTRUMENT] 목록에서 발견: {symbol}")
                    return item
        
        logger.error(f"[INSTRUMENT] 심볼 찾을 수 없음: {symbol}")
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
            logger.info(f"[POSITIONS] response={response.text[:200]}")
            return response.json()
        except Exception as e:
            logger.error(f"[POSITIONS] 오류: {e}")
            return {"code": "error", "msg": str(e)}

    def close_position(self, symbol, side="both"):
        """포지션 청산"""
        positions = self.get_positions(symbol)
        logger.info(f"[CLOSE] 포지션 조회: {positions}")
        
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
        """주문 실행 (디버그 강화)"""
        logger.info(f"[ORDER] 주문 시작: {symbol} {side} {amount}")
        
        # 종목 정보 조회
        inst = self.get_instrument_info(symbol)
        if not inst:
            logger.error(f"[ORDER] 심볼 정보 조회 실패: {symbol}")
            return {"code": "error", "msg": "심볼 정보 조회 실패"}

        lot_size = float(inst.get("lotSz", 0.001))
        min_size = float(inst.get("minSz", 0.001))
        amount = normalize_size(amount, lot_size, min_size)
        logger.info(f"[ORDER] 수량 정규화: {amount} (min:{min_size}, lot:{lot_size})")

        # 계정 설정 조회
        acc_cfg = self.get_account_config()
        pos_mode = (acc_cfg.get("posMode") if acc_cfg else "net_mode")
        logger.info(f"[ORDER] 포지션 모드: {pos_mode}")

        method = "POST"
        path = "/api/v5/trade/order"

        if td_mode is None:
            td_mode = "cash" if self.default_market == "spot" else self.default_tdmode

        body = {
            "instId": symbol,
            "tdMode": td_mode,
            "side": side,
            "ordType": order_type,
            "sz": str(amount)
        }

        if pos_mode == "long_short_mode":
            body["posSide"] = "long" if side == "buy" else "short"

        if price and order_type == "limit":
            body["px"] = str(price)

        body_str = json.dumps(body)
        headers = self.sign_request(method, path, body_str)
        
        logger.info(f"[ORDER] 주문 요청: {body}")
        
        try:
            response = requests.post(self.base_url + path, headers=headers, data=body_str, verify=False, timeout=10)
            logger.info(f"[ORDER] 주문 응답: status={response.status_code} text={response.text}")
            result = response.json()
            return result
        except Exception as e:
            logger.error(f"[ORDER] 주문 실행 오류: {e}")
            return {"code": "error", "msg": str(e)}


# 전역 트레이더
trader = OKXTrader()

# ========= Routes =========
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        webhook_data = request.get_json() if request.is_json else request.get_data(as_text=True)
        logger.info(f"웹훅 수신: {webhook_data}")
        
        parsed = parse_tradingview_webhook(webhook_data, trader)
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


@app.route('/debug', methods=['GET'])
def debug():
    """디버깅용 엔드포인트"""
    try:
        # OKX 공개 API 테스트
        test_url = "https://www.okx.com/api/v5/public/time"
        r = requests.get(test_url, headers=trader.public_headers, verify=False, timeout=10)
        
        return jsonify({
            "okx_public_api": {
                "url": test_url,
                "status": r.status_code,
                "response": r.text[:200],
                "headers": dict(r.headers)
            },
            "environment": {
                "has_api_key": bool(trader.api_key),
                "has_secret": bool(trader.secret_key),
                "has_passphrase": bool(trader.passphrase),
                "base_url": trader.base_url,
                "simulated": trader.simulated
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/positions', methods=['GET'])
def get_positions():
    try:
        positions = trader.get_positions()
        return jsonify(positions)
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


if __name__ == '__main__':
    print("=== TradingView → OKX 자동매매 시스템 시작 (디버그 강화) ===")
    print(f"시뮬레이션 모드: {trader.simulated == '1'}")
    print(f"기본 마켓: {trader.default_market}")
    print(f"기본 거래 모드(tdMode): {trader.default_tdmode}")
    print("웹훅 URL: http://localhost:5000/webhook")
    print("상태 확인: http://localhost:5000/status")
    print("디버그: http://localhost:5000/debug")
    print("=" * 50)

    app.run(host='0.0.0.0', port=5000, debug=True)


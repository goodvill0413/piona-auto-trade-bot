import os
import json
import time
import hmac
import base64
import hashlib
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import logging

# SSL 경고 숨기기
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 환경변수 로드
load_dotenv()

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

class OKXTrader:
    def __init__(self):
        self.api_key = os.getenv('OKX_API_KEY')
        self.secret_key = os.getenv('OKX_API_SECRET')
        self.passphrase = os.getenv('OKX_API_PASSPHRASE')
        self.base_url = os.getenv('OKX_BASE_URL', 'https://www.okx.com')
        self.simulated = os.getenv('OKX_SIMULATED', '1')
        self.default_tdmode = os.getenv('DEFAULT_TDMODE', 'cross')
        self.default_market = os.getenv('DEFAULT_MARKET', 'swap')
        logger.info(f"OKXTrader 초기화 - 시뮬레이션 모드: {self.simulated}, 마켓: {self.default_market}")

    def get_timestamp(self):
    """
    Return ISO8601 UTC timestamp with milliseconds for OKX headers.
    Prefer OKX server time to avoid Invalid OK-ACCESS-TIMESTAMP.
    """
    try:
        r = requests.get(self.base_url + "/api/v5/public/time", timeout=3, verify=False)
        ts_ms = int(r.json()["data"][0]["ts"])  # OKX server timestamp in milliseconds
        from datetime import datetime as _dt
        return _dt.utcfromtimestamp(ts_ms / 1000).isoformat(timespec="milliseconds") + "Z"
    except Exception:
        # Fallback: local UTC time in ISO8601 with milliseconds
        from datetime import datetime as _dt
        return _dt.utcnow().isoformat(timespec="milliseconds") + "Z"

def validate_webhook_token(token):
    """웹훅 토큰 검증"""
    expected_token = os.getenv('WEBHOOK_TOKEN', 'change-me')
    return token == expected_token and token != 'change-me'

def parse_tradingview_webhook(data):
    """TradingView 웹훅 데이터 파싱"""
    try:
        if isinstance(data, dict):
            webhook_data = data
        else:
            webhook_data = json.loads(data)
        required_fields = ['action', 'symbol']
        for field in required_fields:
            if field not in webhook_data:
                raise ValueError(f"필수 필드 누락: {field}")
        
        # 수량 검증
        trader = OKXTrader()
        instrument_info = trader.get_instrument_info(webhook_data['symbol'])
        if instrument_info:
            lot_size = float(instrument_info['lotSz'])
            quantity = float(webhook_data.get('quantity', 0.001))
            if quantity % lot_size != 0:
                adjusted_quantity = round(quantity / lot_size) * lot_size
                logger.warning(f"웹훅 수량({quantity})이 lot size({lot_size})의 배수가 아님. 조정된 수량: {adjusted_quantity}")
                webhook_data['quantity'] = adjusted_quantity
        
        return {
            'action': webhook_data['action'].lower(),
            'symbol': webhook_data['symbol'],
            'quantity': webhook_data.get('quantity', 0.001),
            'price': webhook_data.get('price'),
            'order_type': webhook_data.get('order_type', 'market'),
            'message': webhook_data.get('message', ''),
            'token': webhook_data.get('token', '')
        }
    except Exception as e:
        logger.error(f"웹훅 데이터 파싱 오류: {e}")
        return None

@app.route('/webhook', methods=['POST'])
def webhook():
    """TradingView 웹훅 엔드포인트"""
    try:
        if request.is_json:
            webhook_data = request.get_json()
        else:
            webhook_data = request.get_data(as_text=True)
        logger.info(f"웹훅 수신: {webhook_data}")
        parsed_data = parse_tradingview_webhook(webhook_data)
        if not parsed_data:
            return jsonify({"status": "error", "message": "잘못된 웹훅 데이터"}), 400
        if not validate_webhook_token(parsed_data['token']):
            logger.warning("유효하지 않은 토큰으로 웹훅 요청")
            return jsonify({"status": "error", "message": "유효하지 않은 토큰"}), 403
        action = parsed_data['action']
        symbol = parsed_data['symbol']
        quantity = float(parsed_data['quantity'])
        price = parsed_data.get('price')
        order_type = parsed_data['order_type']
        logger.info(f"실행할 액션: {action}, 심볼: {symbol}, 수량: {quantity}")
        if action in ['buy', 'sell']:
            result = trader.place_order(
                symbol=symbol,
                side=action,
                amount=quantity,
                price=price,
                order_type=order_type
            )
        elif action == 'close':
            result = trader.close_position(symbol, 'both')
        else:
            return jsonify({"status": "error", "message": f"지원하지 않는 액션: {action}"}), 400
        if result['code'] == '0':
            logger.info(f"주문 성공: {result}")
            return jsonify({
                "status": "success",
                "message": f"{action} 주문 실행 완료",
                "data": result
            })
        else:
            logger.error(f"주문 실패: {result}")
            return jsonify({
                "status": "error",
                "message": f"주문 실패: {result.get('msg', '알 수 없는 오류')}"
            }), 500
    except Exception as e:
        logger.error(f"웹훅 처리 오류: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/status', methods=['GET'])
def status():
    """서버 상태 확인"""
    return jsonify({
        "status": "running",
        "timestamp": datetime.now().isoformat(),
        "market": trader.default_market,
        "simulated": trader.simulated == '1'
    })

@app.route('/positions', methods=['GET'])
def get_positions():
    """현재 포지션 조회"""
    try:
        positions = trader.get_positions()
        return jsonify(positions)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/balance', methods=['GET'])
def get_balance():
    """잔고 조회"""
    try:
        method = "GET"
        path = "/api/v5/account/balance"
        headers = trader.sign_request(method, path)
        response = requests.get(
            trader.base_url + path,
            headers=headers,
            verify=False,
            timeout=10
        )
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # 먼저 trader를 만들고
    trader = OKXTrader()
    
    print("=== TradingView → OKX 자동매매 시스템 시작 ===")
    print(f"시뮬레이션 모드: {trader.simulated == '1'}")
    print(f"기본 마켓: {trader.default_market}")
    print(f"기본 거래 모드: {trader.default_tdmode}")
    print("웹훅 URL: http://localhost:5000/webhook")
    print("상태 확인: http://localhost:5000/status")
    print("=" * 50)
    
    # 테스트 코드
    print("=== 규칙 확인 테스트 ===")
    info = trader.get_instrument_info("BTC-USDT-SWAP")
    if info:
        print(f"최소 주문 수량: {info['minSz']}")
        print(f"단위(Lot Size): {info['lotSz']}")
    
    # 웹서버 시작
    app.run(host='0.0.0.0', port=5000, debug=True)

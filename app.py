import os
import hmac
import base64
import hashlib
import json
import time
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# 환경변수 불러오기
load_dotenv()

class OKXTrader:
    def __init__(self):
        self.api_key = os.getenv("OKX_API_KEY")
        self.secret_key = os.getenv("OKX_API_SECRET")
        self.passphrase = os.getenv("OKX_API_PASSPHRASE")
        self.base_url = os.getenv("OKX_BASE_URL", "https://www.okx.com")
        self.simulated = os.getenv("OKX_SIMULATED", "1")  # 기본값 1=데모
        self.td_mode = os.getenv("DEFAULT_TDMODE", "isolated")
        self.market = os.getenv("DEFAULT_MARKET", "swap")
        self.webhook_token = os.getenv("WEBHOOK_TOKEN", "test123")

    def _signature(self, timestamp, method, request_path, body=""):
        message = f"{timestamp}{method}{request_path}{body}"
        mac = hmac.new(self.secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, method, request_path, body=""):
        timestamp = str(time.time())
        sign = self._signature(timestamp, method, request_path, body)
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json"
        }
        if self.simulated == "1":
            headers["x-simulated-trading"] = "1"
        return headers

    def get_balance(self):
        path = "/api/v5/account/balance"
        url = self.base_url + path
        headers = self._headers("GET", path)
        resp = requests.get(url, headers=headers)
        return resp.json()

    def get_positions(self):
        path = "/api/v5/account/positions"
        url = self.base_url + path
        headers = self._headers("GET", path)
        resp = requests.get(url, headers=headers)
        return resp.json()

    def place_order(self, instId, side, ordType="market", sz="1"):
        path = "/api/v5/trade/order"
        url = self.base_url + path
        body = json.dumps({
            "instId": instId,
            "tdMode": self.td_mode,
            "side": side,
            "ordType": ordType,
            "sz": sz
        })
        headers = self._headers("POST", path, body)
        resp = requests.post(url, headers=headers, data=body)
        return resp.json()

# Flask 앱
app = Flask(__name__)

# ✅ Render에서도 쓸 수 있게 전역에서 trader 생성
trader = OKXTrader()

@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "ok": True,
        "use": ["/status", "/balance", "/positions", "/webhook"]
    })

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "market": trader.market,
        "simulated": trader.simulated == "1",
        "status": "running",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")
    })

@app.route("/balance", methods=["GET"])
def balance():
    return jsonify(trader.get_balance())

@app.route("/positions", methods=["GET"])
def positions():
    return jsonify(trader.get_positions())

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON received"}), 400

    token = data.get("token")
    if token != trader.webhook_token:
        return jsonify({"error": "Invalid token"}), 403

    try:
        instId = data["instId"]
        side = data["side"]
        size = str(data.get("size", "1"))
    except KeyError:
        return jsonify({"error": "Missing required parameters"}), 400

    result = trader.place_order(instId, side, sz=size)
    return jsonify(result)

if __name__ == "__main__":
    print("=== TradingView → OKX 자동매매 시스템 시작 ===")
    print(f"시뮬레이션 모드: {trader.simulated == '1'}")
    print(f"기본 마켓: {trader.market}")
    print(f"기본 거래 모드: {trader.td_mode}")
    print(f"웹훅 URL: http://localhost:5000/webhook")
    print(f"상태 확인: http://localhost:5000/status")
    print("==================================================")
    app.run(host="0.0.0.0", port=5000, debug=True)

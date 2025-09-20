import os, hmac, base64, hashlib, json, time, requests, traceback
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

class OKXTrader:
    def __init__(self):
        self.api_key = os.getenv("OKX_API_KEY")
        self.secret_key = os.getenv("OKX_API_SECRET")
        self.passphrase = os.getenv("OKX_API_PASSPHRASE")
        self.base_url = os.getenv("OKX_BASE_URL", "https://www.okx.com")
        self.simulated = os.getenv("OKX_SIMULATED", "1")  # "1"이면 데모
        self.td_mode = os.getenv("DEFAULT_TDMODE", "isolated")
        self.market = os.getenv("DEFAULT_MARKET", "swap")
        self.webhook_token = os.getenv("WEBHOOK_TOKEN", "test123")

    def _env_ok(self):
        missing = []
        if not self.api_key: missing.append("OKX_API_KEY")
        if not self.secret_key: missing.append("OKX_API_SECRET")
        if not self.passphrase: missing.append("OKX_API_PASSPHRASE")
        return missing

    def _signature(self, timestamp, method, request_path, body=""):
        if not self.secret_key:
            raise RuntimeError("Missing OKX_API_SECRET")
        msg = f"{timestamp}{method}{request_path}{body}"
        mac = hmac.new(self.secret_key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, method, request_path, body=""):
        ts = str(time.time())
        sign = self._signature(ts, method, request_path, body)
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if str(self.simulated) == "1":
            headers["x-simulated-trading"] = "1"
        return headers

    def _safe_json(self, resp):
        try:
            return resp.json()
        except Exception:
            return {"code": "HTTP", "status_code": resp.status_code, "text": resp.text[:1000]}

    def get_balance(self):
        miss = self._env_ok()
        if miss:
            return {"code": "ENV", "msg": f"Missing env: {', '.join(miss)}"}
        try:
            path = "/api/v5/account/balance"
            url = self.base_url + path
            headers = self._headers("GET", path)
            r = requests.get(url, headers=headers, timeout=15)
            return self._safe_json(r)
        except Exception as e:
            return {"code": "EXC", "msg": str(e), "trace": traceback.format_exc().splitlines()[-1]}

    def get_positions(self):
        miss = self._env_ok()
        if miss:
            return {"code": "ENV", "msg": f"Missing env: {', '.join(miss)}"}
        try:
            path = "/api/v5/account/positions"
            url = self.base_url + path
            headers = self._headers("GET", path)
            r = requests.get(url, headers=headers, timeout=15)
            return self._safe_json(r)
        except Exception as e:
            return {"code": "EXC", "msg": str(e), "trace": traceback.format_exc().splitlines()[-1]}

    def place_order(self, instId, side, ordType="market", sz="1"):
        miss = self._env_ok()
        if miss:
            return {"code": "ENV", "msg": f"Missing env: {', '.join(miss)}"}
        try:
            path = "/api/v5/trade/order"
            url = self.base_url + path
            body = json.dumps({
                "instId": instId, "tdMode": self.td_mode,
                "side": side, "ordType": ordType, "sz": sz
            })
            headers = self._headers("POST", path, body)
            r = requests.post(url, headers=headers, data=body, timeout=15)
            return self._safe_json(r)
        except Exception as e:
            return {"code": "EXC", "msg": str(e), "trace": traceback.format_exc().splitlines()[-1]}

app = Flask(__name__)

# 전역 생성 (라우트보다 위)
trader = OKXTrader()

@app.route("/", methods=["GET"])
def root():
    return jsonify({"ok": True, "use": ["/status", "/balance", "/positions", "/webhook"]})

@app.route("/status", methods=["GET"])
def status():
    market = getattr(trader, "market", getattr(trader, "default_market", "swap"))
    simulated_raw = getattr(trader, "simulated", "1")
    simulated = (str(simulated_raw) == "1") or (simulated_raw is True)
    return jsonify({
        "market": market,
        "simulated": simulated,
        "status": "running",
        "timestamp": datetime.utcnow().isoformat()
    })

@app.route("/balance", methods=["GET"])
def balance():
    data = trader.get_balance()
    http_code = 200 if isinstance(data, dict) and data.get("code") in (None, "0") else 500
    return jsonify(data), http_code

@app.route("/positions", methods=["GET"])
def positions():
    data = trader.get_positions()
    http_code = 200 if isinstance(data, dict) and data.get("code") in (None, "0") else 500
    return jsonify(data), http_code

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    if token != trader.webhook_token:
        return jsonify({"error": "Invalid token"}), 403
    for k in ("instId", "side"):
        if k not in data:
            return jsonify({"error": f"Missing parameter: {k}"}), 400
    size = str(data.get("size", "1"))
    res = trader.place_order(data["instId"], data["side"], sz=size)
    http_code = 200 if res.get("code") in (None, "0") else 500
    return jsonify(res), http_code

if __name__ == "__main__":
    print("=== TradingView → OKX 자동매매 시스템 시작 ===")
    print(f"시뮬레이션 모드: {trader.simulated == '1'}")
    print(f"기본 마켓: {trader.market}")
    print(f"기본 거래 모드: {trader.td_mode}")
    print(f"웹훅 URL: http://localhost:5000/webhook")
    print(f"상태 확인: http://localhost:5000/status")
    print("==================================================")
    app.run(host="0.0.0.0", port=5000, debug=True)

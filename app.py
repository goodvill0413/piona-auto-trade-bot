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
        self.simulated = os.getenv("OKX_SIMULATED", "1")      # "1"이면 데모
        self.td_mode   = os.getenv("DEFAULT_TDMODE", "isolated")
        self.market    = os.getenv("DEFAULT_MARKET", "swap")
        self.webhook_token = os.getenv("WEBHOOK_TOKEN", "test123")

    def _env_ok(self):
        missing = []
        if not self.api_key:     missing.append("OKX_API_KEY")
        if not self.secret_key:  missing.append("OKX_API_SECRET")
        if not self.passphrase:  missing.append("OKX_API_PASSPHRASE")
        return missing

    def _signature(self, ts, method, path, body=""):
        if not self.secret_key:
            raise RuntimeError("Missing OKX_API_SECRET")
        msg = f"{ts}{method}{path}{body}"
        mac = hmac.new(self.secret_key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, method, path, body=""):
        ts = str(time.time())
        sign = self._signature(ts, method, path, body)
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
            return {"code":"ENV","msg":f"Missing env: {', '.join(miss)}"}
        try:
            path = "/api/v5/account/balance"
            url  = self.base_url + path
            r = requests.get(url, headers=self._headers("GET", path), timeout=15)
            return self._safe_json(r)
        except Exception as e:
            return {"code":"EXC","msg":str(e),"where":"get_balance"}

    def get_positions(self):
        miss = self._env_ok()
        if miss:
            return {"code":"ENV","msg":f"Missing env: {', '.join(miss)}"}
        try:
            path = "/api/v5/account/positions"
            url  = self.base_url + path
            r = requests.get(url, headers=self._headers("GET", path), timeout=15)
            return self._safe_json(r)
        except Exception as e:
            return {"code":"EXC","msg":str(e),"where":"get_positions"}

    def place_order(self, instId, side, ordType="market", sz="1"):
        miss = self._env_ok()
        if miss:
            return {"code":"ENV","msg":f"Missing env: {', '.join(miss)}"}
        try:
            path = "/api/v5/trade/order"
            url  = self.base_url + path
            body = json.dumps({"instId":instId,"tdMode":self.td_mode,"side":side,"ordType":ordType,"sz":sz})
            r = requests.post(url, headers=self._headers("POST", path, body), data=body, timeout=15)
            return self._safe_json(r)
        except Exception as e:
            return {"code":"EXC","msg":str(e),"where":"place_order"}

app = Flask(__name__)

# ✅ 요청 시점마다 최신 환경값으로 생성
def get_trader() -> OKXTrader:
    return OKXTrader()

@app.route("/", methods=["GET"])
def root():
    return jsonify({"ok": True, "use": ["/status", "/balance", "/positions", "/webhook"]})

@app.route("/status", methods=["GET"])
def status():
    t = get_trader()
    return jsonify({
        "market": getattr(t, "market", "swap"),
        "simulated": str(getattr(t, "simulated", "1")) == "1",
        "status": "running",
        "timestamp": datetime.utcnow().isoformat()
    })

@app.route("/balance", methods=["GET"])
def balance():
    t = get_trader()
    data = t.get_balance()
    return jsonify(data), (200 if data.get("code") in (None, "0") else 200)  # 항상 JSON 반환

@app.route("/positions", methods=["GET"])
def positions():
    t = get_trader()
    data = t.get_positions()
    return jsonify(data), (200 if data.get("code") in (None, "0") else 200)

@app.route("/webhook", methods=["POST"])
def webhook():
    t = get_trader()
    data = request.get_json(silent=True) or {}
    if data.get("token") != t.webhook_token:
        return jsonify({"error": "Invalid token"}), 403
    for k in ("instId","side"):
        if k not in data:
            return jsonify({"error": f"Missing parameter: {k}"}), 400
    res = t.place_order(data["instId"], data["side"], sz=str(data.get("size","1")))
    return jsonify(res), (200 if res.get("code") in (None,"0") else 200)

if __name__ == "__main__":
    tt = get_trader()
    print("=== TradingView → OKX 자동매매 시스템 시작 ===")
    print(f"시뮬레이션 모드: {str(tt.simulated)=='1'}")
    print(f"기본 마켓: {tt.market}")
    print(f"기본 거래 모드: {tt.td_mode}")
    print("==================================================")
    app.run(host="0.0.0.0", port=5000, debug=True)

import os
import time
import hmac
import base64
import json
from flask import Flask, request, jsonify
import requests
from urllib.parse import urljoin

app = Flask(__name__)

class OKXTrader:
    def __init__(self):
        self.api_key = os.getenv("OKX_API_KEY")
        self.secret_key = os.getenv("OKX_API_SECRET")
        self.passphrase = os.getenv("OKX_API_PASSPHRASE")
        # ✅ 공백 제거 + 끝 슬래시 제거
        self.base_url = (os.getenv("OKX_BASE_URL", "https://www.okx.com") or "").strip().rstrip("/")
        self.simulated = os.getenv("OKX_SIMULATED", "1")
        self.td_mode = os.getenv("DEFAULT_TDMODE", "isolated")
        self.market = os.getenv("DEFAULT_MARKET", "swap")
        self.webhook_token = os.getenv("WEBHOOK_TOKEN", "test123")

    def _signature(self, timestamp, method, request_path, body=""):
        message = f"{timestamp}{method}{request_path}{body}"
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            digestmod="sha256"
        )
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
            # ✅ 추가된 부분
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; PionaBot/1.0; +https://render.com)"
        }
        if str(self.simulated) == "1":
            headers["x-simulated-trading"] = "1"
        return headers

    def _request(self, method, path, body=""):
        url = urljoin(self.base_url, path)
        headers = self._headers(method, path, body)
        resp = requests.request(method, url, headers=headers, data=body)
        try:
            return resp.json()
        except Exception:
            return {"code": "HTTP", "status_code": resp.status_code, "text": resp.text}

    # ✅ Balance
    def get_balance(self):
        return self._request("GET", "/api/v5/account/balance")

    # ✅ Positions
    def get_positions(self, inst_type="SWAP"):
        return self._request("GET", f"/api/v5/account/positions?instType={inst_type}")


# =========================
# Flask Routes
# =========================
trader = OKXTrader()

@app.route("/")
def home():
    return "PIONA Auto Trade Bot is running!"

@app.route("/status")
def status():
    return jsonify({
        "market": trader.market,
        "simulated": str(trader.simulated) == "1",
        "status": "running",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    })

@app.route("/balance")
def balance():
    return jsonify(trader.get_balance())

@app.route("/positions")
def positions():
    return jsonify(trader.get_positions())


# =========================
# Entry Point
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

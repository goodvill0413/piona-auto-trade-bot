import os, time, hmac, base64, json, traceback
from flask import Flask, request, jsonify
import requests
from urllib.parse import urljoin

app = Flask(__name__)

def env(name, default=None):
    v = os.getenv(name, default)
    return (v or "").strip()

class OKXTrader:
    def __init__(self):
        self.api_key     = env("OKX_API_KEY")
        self.secret_key  = env("OKX_API_SECRET")
        self.passphrase  = env("OKX_API_PASSPHRASE")
        # ✅ 오직 www.okx.com만 사용 (폴백 없음)
        self.base_url    = env("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
        self.simulated   = env("OKX_SIMULATED", "1")
        self.td_mode     = env("DEFAULT_TDMODE", "isolated")
        self.market      = env("DEFAULT_MARKET", "swap")
        self.webhook_token = env("WEBHOOK_TOKEN", "test123")

        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; PionaBot/1.0; +https://render.com)"
        })
        self._timeout = 20

    def _missing_envs(self):
        miss = []
        if not self.api_key:    miss.append("OKX_API_KEY")
        if not self.secret_key: miss.append("OKX_API_SECRET")
        if not self.passphrase: miss.append("OKX_API_PASSPHRASE")
        return miss

    def _signature(self, ts, method, path, body=""):
        msg = f"{ts}{method}{path}{body}"
        mac = hmac.new(self.secret_key.encode("utf-8"), msg.encode("utf-8"), digestmod="sha256")
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, method, path, body=""):
        ts = str(time.time())
        sign = self._signature(ts, method, path, body)
        h = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if str(self.simulated) == "1":
            h["x-simulated-trading"] = "1"
        return h

    def _request(self, method, path, body_obj=None):
        miss = self._missing_envs()
        if miss:
            return {"code":"ENV","msg":f"Missing env: {', '.join(miss)}"}

        body = None if body_obj is None else json.dumps(body_obj)
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        try:
            resp = self._session.request(
                method, url,
                headers=self._headers(method, path, body or ""),
                data=body if body else None,
                timeout=self._timeout,
            )
            try:
                data = resp.json()
            except Exception:
                data = {"code":"HTTP","http_status": resp.status_code, "text": (resp.text or "")[:2000]}
            data.setdefault("http_status", resp.status_code)
            return data
        except requests.exceptions.RequestException as e:
            return {"code":"EXC","msg":str(e)}

    def get_balance(self):
        return self._request("GET", "/api/v5/account/balance")

    def get_positions(self, inst_type="SWAP"):
        return self._request("GET", f"/api/v5/account/positions?instType={inst_type}")

def get_trader():
    return OKXTrader()

@app.route("/", methods=["GET"])
def home():
    return jsonify({"ok": True, "use": ["/status", "/balance", "/positions", "/webhook"]})

@app.route("/status", methods=["GET"])
def status():
    t = get_trader()
    return jsonify({
        "market": t.market or "swap",
        "simulated": str(t.simulated) == "1",
        "status": "running",
        "base_url": t.base_url,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    })

@app.route("/balance", methods=["GET"])
def balance():
    t = get_trader()
    return jsonify(t.get_balance()), 200

@app.route("/positions", methods=["GET"])
def positions():
    t = get_trader()
    return jsonify(t.get_positions()), 200

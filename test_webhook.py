import requests

url = "http://localhost:5000/webhook"
data = {
    "action": "buy",
    "symbol": "BTC-USDT",
    "quantity": 10,
    "token": "test123"
}

try:
    print("요청 전송 중...")
    response = requests.post(url, json=data, timeout=10)  # 10초 타임아웃 추가
    print(f"응답 코드: {response.status_code}")
    print(f"응답 내용: {response.text}")
except requests.exceptions.Timeout:
    print("타임아웃 오류: 서버 응답이 없습니다")
except requests.exceptions.ConnectionError:
    print("연결 오류: 서버에 연결할 수 없습니다")
except Exception as e:
    print(f"기타 오류: {e}")
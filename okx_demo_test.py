import os
import requests
import time
import hmac
import base64
import hashlib
import json
from dotenv import load_dotenv

# SSL 경고 숨기기
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

def test_okx_demo():
    api_key = os.getenv('OKX_API_KEY')
    secret_key = os.getenv('OKX_SECRET_KEY')
    passphrase = os.getenv('OKX_PASSPHRASE')
    
    print("=== OKX 데모 API 테스트 ===")
    
    # 1. 퍼블릭 API (BTC 가격)
    print("\n1. BTC 현재가 조회...")
    try:
        response = requests.get(
            "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT",
            verify=False,
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            if data['code'] == '0':
                price = data['data'][0]['last']
                print(f"✅ BTC-USDT: ${price}")
            else:
                print(f"❌ API 오류: {data}")
        
    except Exception as e:
        print(f"❌ 오류: {e}")
    
    # 2. 프라이빗 API (계정 정보)
    print("\n2. 데모 계정 정보 조회...")
    try:
        timestamp = str(int(time.time()))
        method = "GET"
        path = "/api/v5/account/balance"
        message = timestamp + method + path
        
        signature = base64.b64encode(
            hmac.new(
                secret_key.encode(), 
                message.encode(), 
                hashlib.sha256
            ).digest()
        ).decode()
        
        headers = {
            'OK-ACCESS-KEY': api_key,
            'OK-ACCESS-SIGN': signature,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': passphrase,
            'Content-Type': 'application/json',
            'x-simulated-trading': '1'  # 데모 거래 필수!
        }
        
        response = requests.get(
            "https://www.okx.com" + path,
            headers=headers,
            verify=False,
            timeout=10
        )
        
        print(f"응답 코드: {response.status_code}")
        data = response.json()
        
        if data['code'] == '0':
            print("✅ 데모 계정 연결 성공!")
            if data['data']:
                total_eq = data['data'][0].get('totalEq', '0')
                print(f"   데모 총 자산: ${total_eq}")
                
                # 잔고 상세 정보
                details = data['data'][0].get('details', [])
                for detail in details[:3]:  # 처음 3개만 표시
                    ccy = detail.get('ccy', '')
                    bal = detail.get('bal', '0')
                    if float(bal) > 0:
                        print(f"   {ccy}: {bal}")
            else:
                print("   계정 데이터 없음")
        else:
            print(f"❌ 계정 API 오류: {data}")
            
    except Exception as e:
        print(f"❌ 계정 조회 오류: {e}")
    
    print("\n=== 테스트 완료 ===")
    print("성공하면 트레이딩 봇 개발 준비 완료!")

if __name__ == "__main__":
    test_okx_demo()
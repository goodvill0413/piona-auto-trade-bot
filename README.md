# PIONA Webhook Server

트레이딩뷰 신호를 받아서 바이낸스 테스트넷 자동매매를 실행하는 Flask 웹훅 서버

## 🚀 배포 환경
- **Render.com** 에서 웹 서비스로 배포
- **Flask** 기반 웹훅 서버
- **Gunicorn** WSGI 서버로 운영

## 📡 API 엔드포인트

### GET /
- 서버 상태 확인
- 바이낸스 연결 상태 표시

### POST /webhook
- 트레이딩뷰 신호 수신 및 실제 자동매매 실행
- 바이낸스 선물 테스트넷에서 Market 주문 실행

### GET /health
- 헬스체크 엔드포인트

### GET /balance
- 테스트넷 계좌 잔고 조회 (테스트용)

## 📋 트레이딩뷰 웹훅 페이로드 예시
```json
{
  "signal": "buy",
  "quantity": 0.001,
  "symbol": "BTCUSDT"
}

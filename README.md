# SmartInventory Web

Vercel 배포용 FastAPI 웹 앱입니다. 데스크톱 빌드 파일과 로컬 실행파일 산출물은 제거했습니다.

## 구조

- `api/index.py`: Vercel Python 진입점
- `inventory_web/`: 웹 라우트, 정적 파일, 템플릿
- `inventory_app/`: 웹에서 재사용하는 커넥터/서비스/모델
- `vercel.json`: Vercel 라우팅 설정

## 로컬 실행

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m inventory_web --host 0.0.0.0 --port 8766
```

같은 Wi-Fi의 모바일에서는 맥의 LAN IP로 접속합니다.

```text
http://<mac-lan-ip>:8766/
```

## Vercel 환경변수

필수:

- `SMARTINVENTORY_MONITOR_URL`: Raspberry Pi/monitor 백엔드의 public HTTPS URL
- `DISCORD_INVENTORY_WEBHOOK_URL`: 상품재고 일보를 받을 Discord Webhook URL
- `CRON_SECRET`: Vercel Cron 인증용 랜덤 문자열

선택:

- `SMARTINVENTORY_MONITOR_URL_GIST`: tunnel URL 갱신용 raw gist URL
- `DISCORD_WEBHOOK_URL`: `DISCORD_INVENTORY_WEBHOOK_URL` 대신 쓸 수 있는 공용 Discord Webhook URL

## 상품재고 일보

Vercel Cron은 매일 `0 15 * * *` UTC에 `/api/reports/inventory/discord`를 호출합니다. 한국시간으로는 매일 00:00입니다.

수동 미리보기:

```text
/api/reports/inventory/discord?dry_run=1
```

보고서는 상품관리의 마스터 상품만 포함하며, 미연결 네이버/쿠팡 채널 상품은 제외합니다.

`config/credentials.json`과 로컬 SQLite DB는 Git/Vercel 배포 대상에서 제외됩니다.

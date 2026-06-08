# SmartInventory Cloudflare Deployment

Cloudflare에는 FastAPI 앱을 그대로 Pages 정적 사이트처럼 올리지 않습니다. 이 프로젝트는 Python 서버, 로컬 캐시, Pi/monitor 백엔드 연동이 있으므로 Cloudflare Tunnel로 공개하고, 자정 디스코드 일보는 Cloudflare Worker Cron이 터널 주소를 호출하는 방식이 가장 단순합니다.

## 1. 앱 실행

Discord Webhook URL과 Cron 비밀키를 환경변수로 넣고 앱을 실행합니다.

```bash
export DISCORD_INVENTORY_WEBHOOK_URL="디스코드에서_복사한_웹훅_URL"
export CRON_SECRET="긴_랜덤_문자열"
python3 -m inventory_web --host 127.0.0.1 --port 8766
```

Pi/monitor 백엔드를 따로 쓰는 배포라면 `SMARTINVENTORY_MONITOR_URL`도 같이 넣습니다.

```bash
export SMARTINVENTORY_MONITOR_URL="https://monitor.example.com"
```

## 2. Cloudflare Tunnel 만들기

Cloudflare Dashboard에서 진행합니다.

1. Cloudflare에 로그인합니다.
2. `Zero Trust`로 들어갑니다.
3. `Networks` -> `Tunnels`를 엽니다.
4. `Create a tunnel`을 누릅니다.
5. Connector는 `cloudflared`를 선택합니다.
6. Tunnel 이름은 `smartinventory`처럼 입력합니다.
7. 화면에 나오는 설치/실행 명령을 앱이 실행되는 Mac 또는 Raspberry Pi에서 실행합니다.
8. `Public Hostname`을 추가합니다.
9. Subdomain/Domain을 정합니다. 예: `inventory.example.com`
10. Service Type은 `HTTP`로 선택합니다.
11. Service URL은 `127.0.0.1:8766`으로 입력합니다.
12. 저장 후 `https://inventory.example.com`으로 접속되는지 확인합니다.

Mac이 잠자기 상태가 되면 앱도 내려갑니다. 계속 켜둘 목적이면 Raspberry Pi나 항상 켜져 있는 서버에서 실행하는 쪽이 맞습니다.

## 3. 디스코드 일보 수동 테스트

터널 도메인에서 먼저 미리보기를 확인합니다.

```bash
curl -H "Authorization: Bearer 긴_랜덤_문자열" \
  "https://inventory.example.com/api/reports/inventory/discord?dry_run=1"
```

실제 디스코드 전송 테스트:

```bash
curl -H "Authorization: Bearer 긴_랜덤_문자열" \
  "https://inventory.example.com/api/reports/inventory/discord"
```

## 4. Cloudflare Worker Cron 배포

이 repo에는 Cron 호출용 Worker 템플릿이 들어 있습니다.

- `cloudflare/discord-cron-worker.mjs`
- `wrangler.toml`

Cloudflare CLI 로그인:

```bash
npx wrangler login
```

Worker에 환경변수/시크릿을 넣습니다.

```bash
npx wrangler secret put APP_URL
npx wrangler secret put CRON_SECRET
```

입력값:

- `APP_URL`: `https://inventory.example.com`
- `CRON_SECRET`: 앱 실행 시 넣은 `CRON_SECRET`과 같은 값

배포:

```bash
npx wrangler deploy
```

`wrangler.toml`의 Cron은 `0 15 * * *`입니다. Cloudflare Cron은 UTC 기준이므로 한국시간 매일 00:00에 실행됩니다.

## 5. Cloudflare Pages에 바로 올리지 않는 이유

Cloudflare Pages는 정적 사이트/Pages Functions 중심입니다. Cloudflare Workers는 Python과 FastAPI를 지원하지만, 이 프로젝트는 현재 로컬 SQLite 캐시와 서버 파일시스템을 쓰는 코드 경로가 있어 Worker로 바로 옮기려면 별도 리팩터링이 필요합니다.

지금 운영에는 Tunnel 방식이 가장 덜 위험합니다.

# 번역기 배포: 라즈베리파이 + Vercel 재고 사이트

번역 서버는 재고 서버가 있는 라즈베리파이에서 상시 실행하고, Vercel 재고 사이트 우측 상단의 **번역기** 버튼으로 들어갑니다.
재고 사이트 비밀번호로 로그인되어 있으면 번역기에는 따로 로그인하지 않습니다.

```
[브라우저] 재고 사이트(Vercel) 로그인 → '번역기' 클릭
     │  /translator: gist 에서 파이 번역기 주소를 읽고, 2분짜리 서명 토큰을 붙여 이동
     ▼
[라즈베리파이] Cloudflare quick tunnel → kr-translator (127.0.0.1:8777)
     └ Codex app-server (ChatGPT 로그인) → GPT 번역
```

Vercel 에 번역 서버를 직접 올리지 않는 이유: ChatGPT 로그인을 쥐고 있는 Codex 프로세스가 계속 켜져 있어야 하고, Codex 실행 파일만 281MB 입니다.

## 준비물

재고 서버 설치 때 이미 갖춘 것들입니다.

- 64비트 Raspberry Pi OS (Codex CLI 는 32비트 ARM 빌드가 없습니다)
- `/usr/local/bin/cloudflared`
- 파이의 `~/.github_gist_token` (재고 서버 주소를 gist 에 올릴 때 쓰는 토큰)
- Vercel 의 `SMARTINVENTORY_WEB_PASSWORD` (사이트 비밀번호가 곧 번역기 열쇠입니다. 없으면 번역기 메뉴가 동작하지 않습니다)

## 1. 파이에 설치 (Mac 에서 한 줄)

```bash
./translator/deploy/deploy_pi.sh beiko@raspberrypi.local /home/beiko/st https://<vercel-도메인>/translator
```

코드를 파이로 보낸 뒤 파이에서 `translator/deploy/install.sh` 를 실행합니다.

- Python 패키지, Node.js, Codex CLI 설치
- 서비스 등록: `kr-translator`(번역 서버), `kr-translator-tunnel`(터널), `publish-translator-url.timer`(2분마다 터널 주소를 gist 의 `translator.json` 에 게시)
- `/etc/kr-translator.env` 생성 (공유 비밀키 포함, 권한 600)

마지막에 출력되는 `TRANSLATOR_SHARED_SECRET=...` 값을 복사해 두세요.

## 2. 파이에서 ChatGPT 로그인 (처음 한 번)

```bash
ssh beiko@raspberrypi.local
codex login --device-auth
sudo systemctl restart kr-translator
```

화면에 나온 주소에서 코드를 입력하면 됩니다. 번역기 화면의 **기기 코드로 로그인** 버튼으로 해도 됩니다(이 경우 재시작 불필요).

- 코드를 입력했는데 로그인이 안 되면 ChatGPT 설정의 보안 항목에서 **Codex 기기 코드 인증**을 켜야 합니다. 회사(Business/Enterprise) 계정은 관리자가 허용해야 합니다.
  이 내용은 여러 외부 가이드가 같은 설명을 하고 있지만 OpenAI 공식 문서는 직접 확인하지 못했습니다.
- 브라우저 로그인 버튼은 서버에서는 쓸 수 없습니다. 로그인 완료 주소가 파이 자신의 `127.0.0.1:1455` 로 돌아가기 때문입니다.

## 3. Vercel 환경변수

| 이름 | 값 |
| --- | --- |
| `TRANSLATOR_SHARED_SECRET` | 1단계에서 출력된 값 (파이의 `/etc/kr-translator.env` 와 같아야 함) |
| `TRANSLATOR_URL_GIST` | 선택. `SMARTINVENTORY_MONITOR_URL_GIST` 가 있으면 같은 gist 의 `translator.json` 을 자동으로 씁니다 |
| `TRANSLATOR_URL` | 선택. 고정 도메인(named tunnel)을 쓸 때만 |

환경변수를 바꾼 뒤에는 Vercel 에서 다시 배포(Redeploy)해야 적용됩니다. 이 코드가 Vercel 이 배포하는 브랜치에 들어가 있어야 메뉴가 보입니다.

## 4. 사용

재고 사이트 우측 상단 **번역기** → 새 탭에 번역기가 열립니다.

- 번역기 세션은 30일 유지됩니다. 파이 재부팅으로 터널 주소가 바뀌면 메뉴로 다시 들어오면 됩니다.
- 화면 갱신은 롱폴링입니다. Cloudflare quick tunnel 은 Server-Sent Events 를 지원하지 않기 때문입니다(Cloudflare 문서). 새 글자가 생기는 즉시 응답하므로 체감상 실시간입니다.

## 업데이트

1단계 명령을 다시 실행하면 됩니다. 비밀키와 설정은 유지되고, 터널은 재시작하지 않아 주소도 그대로입니다.
Codex CLI 는 자동으로 올리지 않습니다. 올리려면 파이에서:

```bash
sudo UPDATE_CODEX=1 bash ~/st/translator/deploy/install.sh
```

## 문제 해결

| 증상 | 확인 |
| --- | --- |
| "번역 서버 주소를 찾지 못했습니다" | `sudo journalctl -u publish-translator-url -n 20`, `systemctl status kr-translator-tunnel` |
| "링크가 만료되었습니다" | 메뉴를 다시 누르세요. 반복되면 파이 시계(NTP) 확인: `timedatectl` |
| 로그인 창이 계속 뜸 | 파이에서 `codex login status` |
| 번역 오류 | `sudo journalctl -u kr-translator -n 50` |
| 터널이 안 뜸 | `~/.cloudflared/config.yaml` 이 있으면 quick tunnel 이 동작하지 않습니다(Cloudflare 문서) |

고정 주소를 원하면 Cloudflare 계정과 도메인으로 named tunnel 을 만들고, Vercel 에 `TRANSLATOR_URL`, 파이의 `/etc/kr-translator.env` 에 `TRANSLATOR_ALLOWED_HOSTS=<그 도메인>` 을 넣으세요.

## 보안

- 파이의 번역 서버는 `127.0.0.1` 에만 열리고 터널로만 노출됩니다. 재고 사이트가 서명한 토큰이나 그 토큰으로 받은 세션 쿠키가 없으면 모든 요청을 거절합니다.
- 비밀키를 바꾸려면 `/etc/kr-translator.env` 와 Vercel 값을 함께 바꾸고 `sudo systemctl restart kr-translator` 후 Vercel 을 다시 배포하세요.
- 게시 스크립트가 죽은 터널을 재시작할 수 있도록 `/etc/sudoers.d/kr-translator` 에 `systemctl restart kr-translator-tunnel.service` 한 가지만 허용합니다.

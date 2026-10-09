# 번역기 배포: 라즈베리파이 + Vercel Beiko 사이트

번역 서버는 재고 서버가 있는 라즈베리파이에서 상시 실행하고, Vercel Beiko 사이트 우측 상단의 **번역기** 버튼으로 들어갑니다.
`https://www.beiko.co.kr/translator` 링크만 있으면 사이트 로그인 없이 사용할 수 있습니다.

```
[브라우저] Beiko 사이트(Vercel) '번역기' 클릭 또는 공유 링크 열기
     │  /translator: gist 에서 파이 번역기 주소를 읽고, 2분짜리 서명 토큰을 붙여 이동
     ▼
[라즈베리파이] Cloudflare quick tunnel → kr-translator (127.0.0.1:8777)
     └ Codex app-server (ChatGPT 로그인) → GPT 번역
```

Vercel 에 번역 서버를 직접 올리지 않는 이유: ChatGPT 로그인을 쥐고 있는 Codex 프로세스가 계속 켜져 있어야 하고, Codex 실행 파일만 281MB 입니다.

## 준비물

재고 서버 설치 때 이미 갖춘 것들입니다.

- 64비트 커널 (Codex CLI 는 32비트 ARM 빌드가 없습니다). 32비트 사용자 환경에서는 아래 ARM64 실행 파일 설정을 사용합니다.
- `/usr/local/bin/cloudflared`
- 파이의 `~/.github_gist_token` (재고 서버 주소를 gist 에 올릴 때 쓰는 토큰)
- Vercel 의 `SMARTINVENTORY_WEB_PASSWORD` (사이트 비밀번호가 곧 번역기 열쇠입니다. 없으면 번역기 메뉴가 동작하지 않습니다)

## 1. 파이에 설치 (Mac 에서 한 줄)

```bash
./translator/deploy/deploy_pi.sh beiko@raspberrypi.local /home/beiko/st https://<vercel-도메인>/translator
```

코드를 파이로 보낸 뒤 파이에서 `translator/deploy/install.sh` 를 실행합니다.

- 재고 서버와 분리된 Python 가상 환경에 패키지 설치, Node.js와 Codex CLI 확인
- 서비스 등록: `kr-translator`(번역 서버), `kr-translator-tunnel`(터널), `publish-translator-url.timer`(2분마다 터널 주소를 gist 의 `translator.json` 에 게시)
- `/etc/kr-translator.env` 생성 (공유 비밀키 포함, 권한 600)

마지막에 출력되는 `TRANSLATOR_SHARED_SECRET=...` 값을 복사해 두세요.

### 64비트 커널 + 32비트 Node.js

`uname -m` 이 `aarch64` 여도 `node -p process.arch` 가 `arm` 이면 npm Codex는 실행되지 않습니다.
OpenAI 공식 릴리스의 `codex-aarch64-unknown-linux-musl` 실행 파일은 Node.js 없이 실행할 수 있습니다.
파이에서 실행 파일을 설치한 뒤 최초 설치에 경로를 지정하세요:

```bash
sudo TRANSLATOR_CODEX_BIN=/path/to/codex-aarch64-unknown-linux-musl bash ~/st/translator/deploy/install.sh https://www.beiko.co.kr/translator
```

설치 후에는 `/etc/kr-translator.env` 의 실행 파일 경로를 재사용하므로 일반 업데이트 명령으로 유지됩니다.
Python 환경은 기본적으로 `~/.local/lib/kr-translator/venv` 에 생성됩니다.

## 2. 파이에서 ChatGPT 로그인 (처음 한 번)

```bash
ssh beiko@raspberrypi.local
codex login --device-auth
sudo systemctl restart kr-translator
```

관리자가 화면에 나온 주소에서 코드를 입력하면 됩니다. 공개 번역기에서는 서버 계정을 변경할 수 없습니다.
32비트 Node 환경에서 별도 Codex 실행 파일을 설치했다면 `/etc/kr-translator.env`의 `TRANSLATOR_CODEX_BIN` 경로를 사용하세요.

- 코드를 입력했는데 로그인이 안 되면 ChatGPT 설정의 보안 항목에서 **Codex 기기 코드 인증**을 켜야 합니다. 회사(Business/Enterprise) 계정은 관리자가 허용해야 합니다.
  [OpenAI 공식 인증 문서](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)에서 확인할 수 있습니다.
- 브라우저 로그인 버튼은 서버에서는 쓸 수 없습니다. 로그인 완료 주소가 파이 자신의 `127.0.0.1:1455` 로 돌아가기 때문입니다.

## 3. Vercel 환경변수

`www.beiko.co.kr` 은 이 저장소의 Python 웹 앱이 아니라 `beiko3444/beico-app` 의 Next.js 앱입니다.
해당 사이트의 `/translator` 경로는 로그인 없이 방문자에게 서명 링크를 발급하고 파이로 연결합니다.
`st` 브랜치 머지만으로는 이 사이트에 버튼이 추가되지 않으므로 `beico-app` 의 연결 코드도 배포해야 합니다.
Next.js 번역기 연결에는 `SMARTINVENTORY_WEB_PASSWORD`나 관리자 로그인이 필요하지 않습니다. Python 재고 앱의 기존 로그인 정책은 별도입니다.

| 이름 | 값 |
| --- | --- |
| `TRANSLATOR_SHARED_SECRET` | 1단계에서 출력된 값 (파이의 `/etc/kr-translator.env` 와 같아야 함) |
| `TRANSLATOR_URL_GIST` | 선택. `SMARTINVENTORY_MONITOR_URL_GIST` 가 있으면 같은 gist 의 `translator.json` 을 자동으로 씁니다 |
| `TRANSLATOR_URL` | 선택. 고정 도메인(named tunnel)을 쓸 때만 |

환경변수를 바꾼 뒤에는 Vercel 에서 다시 배포(Redeploy)해야 적용됩니다. 이 코드가 Vercel 이 배포하는 브랜치에 들어가 있어야 메뉴가 보입니다.

## 4. 사용

기본 모델은 `gpt-6-luna`이며, 외국어 결과와 한국어 확인 번역에 모두 사용합니다.
운영 모델은 `/etc/kr-translator.env`의 `TRANSLATOR_MODEL`로 지정할 수 있습니다.
설정 변경 후 `sudo systemctl restart kr-translator`로 반영합니다.

Beiko 메뉴의 **번역기** 아이콘 또는 `https://www.beiko.co.kr/translator` → 번역기가 열립니다.
영어·중국어 결과 아래에는 각 외국어 결과를 다시 한국어로 번역한 확인본이 표시됩니다.

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

- 파이의 번역 서버는 `127.0.0.1` 에만 열리고 터널로만 노출됩니다. Beiko의 공개 링크가 서명한 토큰으로 방문자 세션을 발급합니다. 세션이 없으면 공유 링크로 연결하며 별도 로그인을 요구하지 않습니다.
- 배포 모드의 웹 계정 로그인·로그아웃 API는 차단되며, 서버 계정 정보와 사용량을 방문자에게 노출하지 않습니다. 계정 관리는 SSH에서만 합니다.
- 비밀키를 바꾸려면 `/etc/kr-translator.env` 와 Vercel 값을 함께 바꾸고 `sudo systemctl restart kr-translator` 후 Vercel 을 다시 배포하세요.
- 게시 스크립트가 죽은 터널을 재시작할 수 있도록 `/etc/sudoers.d/kr-translator` 에 `systemctl restart kr-translator-tunnel.service` 한 가지만 허용합니다.

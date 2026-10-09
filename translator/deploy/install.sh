#!/usr/bin/env bash
#
# 라즈베리파이에 KR 즉시 번역기 설치/업데이트
#   - kr-translator          : 번역 서버 (127.0.0.1:8777)
#   - kr-translator-tunnel   : cloudflared quick tunnel
#   - publish-translator-url : 터널 주소를 gist 의 translator.json 에 게시 (2분마다)
#
# 사용법: sudo bash translator/deploy/install.sh [포털 URL]
#   포털 URL 예: https://<vercel-도메인>/translator
#   보통은 Mac 에서 translator/deploy/deploy_pi.sh 로 실행합니다.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
CURRENT_USER="${SUDO_USER:-$USER}"
USER_HOME="$(getent passwd "$CURRENT_USER" | cut -d: -f6)"
ENV_FILE=/etc/kr-translator.env
PORT=8777
PORTAL_URL="${1:-${TRANSLATOR_PORTAL_URL:-}}"

if [ "$(id -u)" -ne 0 ]; then
    echo "sudo 로 실행하세요: sudo bash $0 [포털 URL]"
    exit 1
fi

echo "=== KR 즉시 번역기 설치 ==="
echo "프로젝트 경로: $PROJECT_DIR"
echo "실행 사용자:   $CURRENT_USER"
echo ""

# 1. Codex CLI 는 64비트 빌드만 있음 (linux-arm64 / linux-x64)
echo "[1/7] 시스템 확인..."
case "$(uname -m)" in
    aarch64|arm64|x86_64) ;;
    *)
        echo "ERROR: $(uname -m) 는 지원되지 않습니다. Codex CLI 는 64비트 OS(aarch64)에서만 동작합니다."
        echo "       64비트 Raspberry Pi OS 가 필요합니다."
        exit 1
        ;;
esac
if [ ! -f "$PROJECT_DIR/translator/__main__.py" ]; then
    echo "ERROR: $PROJECT_DIR/translator 가 없습니다."
    exit 1
fi

# 2. 재고 서버의 Python 패키지를 바꾸지 않는 별도 환경
echo "[2/7] Python 패키지 설치..."
VENV_DIR="${TRANSLATOR_VENV_DIR:-$USER_HOME/.local/lib/kr-translator/venv}"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    sudo -u "$CURRENT_USER" -H python3 -m venv "$VENV_DIR"
fi
sudo -u "$CURRENT_USER" -H "$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/translator/requirements.txt"
PYTHON_BIN="$VENV_DIR/bin/python"

# 3. Node.js + Codex CLI (ChatGPT 로그인과 GPT 번역 담당)
echo "[3/7] Codex CLI 확인..."
CODEX_BIN="${TRANSLATOR_CODEX_BIN:-}"
if [ -z "$CODEX_BIN" ] && [ -r "$ENV_FILE" ] && [ "${UPDATE_CODEX:-0}" != "1" ]; then
    CODEX_BIN="$(sed -n 's/^TRANSLATOR_CODEX_BIN=//p' "$ENV_FILE" | head -1)"
fi
if [ -z "$CODEX_BIN" ]; then
    if ! command -v npm >/dev/null 2>&1; then
        apt-get update
        apt-get install -y nodejs npm
    fi
    NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
    if [ "$NODE_MAJOR" -lt 16 ]; then
        echo "ERROR: Node.js 16 이상이 필요합니다 (현재 $(node --version))."
        exit 1
    fi
    # 이미 설치돼 있으면 버전을 바꾸지 않습니다. 올리려면 UPDATE_CODEX=1 로 실행하세요.
    if ! command -v codex >/dev/null 2>&1 || [ "${UPDATE_CODEX:-0}" = "1" ]; then
        npm install -g @openai/codex@latest
    fi
    CODEX_BIN="$(command -v codex)"
fi
if ! CODEX_VERSION="$(sudo -u "$CURRENT_USER" -H "$CODEX_BIN" --version 2>&1)"; then
    echo "ERROR: Codex 를 실행할 수 없습니다: $CODEX_VERSION"
    echo "       64비트 커널에 32비트 Node.js 를 쓰는 경우 ARM64 musl 실행 파일을"
    echo "       TRANSLATOR_CODEX_BIN 으로 지정하세요."
    exit 1
fi
echo "  codex: $CODEX_BIN ($CODEX_VERSION)"

CLOUDFLARED="$(command -v cloudflared || echo /usr/local/bin/cloudflared)"
if [ ! -x "$CLOUDFLARED" ]; then
    echo "ERROR: cloudflared 가 없습니다. 재고 서버 터널과 같은 방식으로 /usr/local/bin/cloudflared 를 설치하세요."
    exit 1
fi

# 4. 설정 파일 (공유 비밀키는 처음 한 번만 생성)
echo "[4/7] 설정 파일 $ENV_FILE ..."
if [ ! -f "$ENV_FILE" ]; then
    SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    (
        umask 077
        cat > "$ENV_FILE" <<EOF
# KR 즉시 번역기 설정. 수정 후: sudo systemctl restart kr-translator
# Vercel 의 TRANSLATOR_SHARED_SECRET 과 같은 값이어야 합니다.
TRANSLATOR_SHARED_SECRET=$SECRET
# Vercel 재고 사이트의 번역기 주소 (세션이 끝나면 여기로 돌려보냄)
TRANSLATOR_PORTAL_URL=$PORTAL_URL
TRANSLATOR_CODEX_BIN=$CODEX_BIN
TRANSLATOR_MODEL=gpt-6-luna
# Jev (선택): 둘 중 하나
# TYPESAFE_API_KEY=
# CLOUDFLARE_ACCOUNT_ID=
# CLOUDFLARE_API_TOKEN=
EOF
    )
    echo "  새 공유 비밀키를 만들었습니다."
else
    if [ -n "$PORTAL_URL" ]; then
        sed -i "s|^TRANSLATOR_PORTAL_URL=.*|TRANSLATOR_PORTAL_URL=$PORTAL_URL|" "$ENV_FILE"
    fi
    sed -i "s|^TRANSLATOR_CODEX_BIN=.*|TRANSLATOR_CODEX_BIN=$CODEX_BIN|" "$ENV_FILE"
    echo "  기존 설정을 유지합니다."
fi

# 5. systemd 서비스
echo "[5/7] 서비스 등록..."
SYSTEMCTL_BIN="$(command -v systemctl)"
cat > /etc/systemd/system/kr-translator.service <<EOF
[Unit]
Description=KR 즉시 번역기 (port $PORT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_DIR}
Environment=HOME=${USER_HOME}
EnvironmentFile=${ENV_FILE}
ExecStart=${PYTHON_BIN} -m translator --host 127.0.0.1 --port ${PORT}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/kr-translator-tunnel.service <<EOF
[Unit]
Description=Cloudflare Tunnel for KR 즉시 번역기
After=network-online.target kr-translator.service
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
ExecStart=${CLOUDFLARED} tunnel --url http://localhost:${PORT} --no-autoupdate
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

install -m 755 "$SCRIPT_DIR/publish-translator-url.sh" /usr/local/bin/publish-translator-url.sh
cat > /etc/systemd/system/publish-translator-url.service <<EOF
[Unit]
Description=Publish KR translator tunnel URL to GitHub gist
After=network-online.target kr-translator-tunnel.service
Wants=network-online.target

[Service]
Type=oneshot
User=${CURRENT_USER}
ExecStart=/usr/local/bin/publish-translator-url.sh
EOF

cat > /etc/systemd/system/publish-translator-url.timer <<EOF
[Unit]
Description=Keep the KR translator tunnel URL in the gist up to date

[Timer]
OnBootSec=45s
OnUnitActiveSec=2min
Persistent=true

[Install]
WantedBy=timers.target
EOF

# 게시 스크립트가 죽은 터널을 재시작할 수 있게 이 명령 하나만 허용
SUDOERS_TMP="$(mktemp)"
echo "${CURRENT_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} restart kr-translator-tunnel.service" > "$SUDOERS_TMP"
if visudo -cf "$SUDOERS_TMP" >/dev/null; then
    install -m 440 "$SUDOERS_TMP" /etc/sudoers.d/kr-translator
fi
rm -f "$SUDOERS_TMP"

# 6. 시작 (터널은 이미 떠 있으면 그대로 둡니다: 재시작하면 주소가 바뀜)
echo "[6/7] 서비스 시작..."
systemctl daemon-reload
systemctl enable kr-translator.service kr-translator-tunnel.service publish-translator-url.timer >/dev/null
systemctl restart kr-translator.service
systemctl start kr-translator-tunnel.service
systemctl start publish-translator-url.timer
if [ -r "$USER_HOME/.github_gist_token" ]; then
    systemctl start --no-block publish-translator-url.service
else
    echo "  경고: $USER_HOME/.github_gist_token 이 없어 터널 주소를 gist 에 올릴 수 없습니다."
    echo "        재고 서버용 publish-tunnel-url 과 같은 토큰 파일을 사용합니다."
fi

# 7. 상태 안내
echo "[7/7] 확인..."
sleep 3
if curl -sSf --max-time 5 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    echo "  번역 서버: 정상"
else
    echo "  번역 서버: 응답 없음 -> sudo journalctl -u kr-translator -n 50"
fi

echo ""
echo "=== 설치 완료 ==="
echo ""
echo "1) ChatGPT 로그인 (처음 한 번):"
if sudo -u "$CURRENT_USER" -H "$CODEX_BIN" login status >/dev/null 2>&1; then
    echo "   이미 로그인되어 있습니다: $(sudo -u "$CURRENT_USER" -H "$CODEX_BIN" login status 2>&1 | head -1)"
else
    echo "   파이에서 실행: codex login --device-auth"
    echo "   (번역기 화면의 '기기 코드로 로그인' 버튼으로도 됩니다)"
fi
echo ""
echo "2) Vercel 환경변수 (처음 한 번):"
echo "   TRANSLATOR_SHARED_SECRET=$(grep '^TRANSLATOR_SHARED_SECRET=' "$ENV_FILE" | cut -d= -f2-)"
echo ""
echo "3) 재고 사이트 우측 상단 '번역기' 버튼으로 접속"
echo ""
echo "유용한 명령어:"
echo "  서버 로그:  sudo journalctl -u kr-translator -f"
echo "  터널 로그:  sudo journalctl -u kr-translator-tunnel -f"
echo "  주소 게시:  sudo journalctl -u publish-translator-url -n 20"

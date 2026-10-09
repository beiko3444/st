#!/usr/bin/env bash
set -euo pipefail

# Mac 에서 실행: 번역기 코드를 라즈베리파이로 보내고 설치/업데이트까지 한 번에 합니다.
# 사용:
#   ./translator/deploy/deploy_pi.sh
#   ./translator/deploy/deploy_pi.sh beiko@raspberrypi.local /home/beiko/st https://<vercel-도메인>/translator

TARGET="${1:-beiko@raspberrypi.local}"
REMOTE_DIR="${2:-/home/beiko/st}"
PORTAL_URL="${3:-${TRANSLATOR_PORTAL_URL:-}}"

cd "$(dirname "$0")/../.."

echo "사용 대상: ${TARGET}"
echo "원격 경로: ${REMOTE_DIR}"

SSH_OPTS=(-o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)
echo "[1/3] 연결 확인: ${TARGET}"
if ! ssh "${SSH_OPTS[@]}" "${TARGET}" "echo connected: \$(hostname) \$(uname -m)"; then
  cat <<'EOF'
연결 실패입니다.
- 사용자명/비밀번호 또는 호스트 이름을 확인하세요.
- 예: ./translator/deploy/deploy_pi.sh <user>@raspberrypi.local /home/<user>/st
EOF
  exit 1
fi

echo "[2/3] 코드 전송 -> ${TARGET}:${REMOTE_DIR}/translator/"
ssh "${SSH_OPTS[@]}" "${TARGET}" "mkdir -p '${REMOTE_DIR}'"
rsync -avz --delete --exclude '__pycache__' \
  translator/ \
  "${TARGET}:${REMOTE_DIR}/translator/"

echo "[3/3] 설치/재시작 (sudo 비밀번호를 물을 수 있습니다)"
ssh -tt "${SSH_OPTS[@]}" "${TARGET}" "sudo bash '${REMOTE_DIR}/translator/deploy/install.sh' '${PORTAL_URL}'"

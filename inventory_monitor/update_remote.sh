#!/usr/bin/env bash
set -euo pipefail

# 라즈베리파이 inventory monitor/server 코드 빠른 업데이트 스크립트
# 사용:
#   ./inventory_monitor/update_remote.sh
#   ./inventory_monitor/update_remote.sh pi@192.168.0.42 /home/pi/st

TARGET="${1:-beiko@raspberrypi.local}"
REMOTE_DIR="${2:-/home/beiko/st}"

echo "사용 대상: ${TARGET}"
echo "원격 경로: ${REMOTE_DIR}"

SSH_OPTS=(-o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)
echo "[1/4] 연결 확인: ${TARGET}"
if ! ssh "${SSH_OPTS[@]}" "${TARGET}" "echo connected: \$(hostname)"; then
  cat <<'EOF'
연결 실패(인증 오류)입니다.
- 보통 원인: 사용자명 또는 비밀번호 불일치
- 라즈베리파이 터미널에서 `whoami`로 사용자명 확인 후 다시 실행:
  ./inventory_monitor/update_remote.sh <user>@raspberrypi.local /home/<user>/st
- 비밀번호 입력 시 화면에 문자가 안 보이는 것은 정상입니다.
EOF
  exit 1
fi

echo "[2/4] 코드 전송 -> ${TARGET}:${REMOTE_DIR}"
rsync -avz --delete \
  inventory_monitor/ \
  "${TARGET}:${REMOTE_DIR}/inventory_monitor/"

echo "[3/4] 서비스 재시작"
ssh -tt "${TARGET}" "sudo systemctl restart inventory-monitor.service inventory-server.service"

echo "[4/4] 상태 확인"
ssh -tt "${TARGET}" "sudo systemctl --no-pager --full status inventory-monitor.service inventory-server.service | sed -n '1,80p'"

echo "완료: 라즈베리 코드 반영 + 서비스 재시작"

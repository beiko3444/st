#!/usr/bin/env bash
#
# 라즈베리파이 재고 모니터링 설치 스크립트
# 사용법: sudo bash install.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_NAME="inventory-monitor"
CURRENT_USER="${SUDO_USER:-$USER}"

echo "=== 재고 모니터링 설치 ==="
echo "프로젝트 경로: $PROJECT_DIR"
echo "실행 사용자:   $CURRENT_USER"
echo ""

# 1. 의존성 설치 (PySide6 불필요)
echo "[1/4] Python 패키지 설치..."
pip3 install --break-system-packages httpx bcrypt 2>/dev/null \
    || pip3 install httpx bcrypt

# 2. credentials.json 확인
if [ ! -f "$PROJECT_DIR/config/credentials.json" ]; then
    echo "ERROR: $PROJECT_DIR/config/credentials.json 파일이 없습니다."
    echo "기존 PC에서 config/credentials.json을 복사해 주세요."
    exit 1
fi

# 3. systemd 서비스 파일 생성
echo "[2/4] systemd 서비스 파일 생성..."
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Smart Inventory Monitor (10분 간격 재고 수집)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=/usr/bin/python3 ${PROJECT_DIR}/inventory_monitor/monitor.py
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 4. 서비스 등록 및 시작
echo "[3/4] 서비스 등록..."
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}.service

echo "[4/4] 서비스 시작..."
systemctl start ${SERVICE_NAME}.service

echo ""
echo "=== 설치 완료! ==="
echo ""
echo "유용한 명령어:"
echo "  상태 확인:  sudo systemctl status ${SERVICE_NAME}"
echo "  로그 보기:  sudo journalctl -u ${SERVICE_NAME} -f"
echo "  중지:       sudo systemctl stop ${SERVICE_NAME}"
echo "  재시작:     sudo systemctl restart ${SERVICE_NAME}"
echo "  제거:       sudo systemctl disable ${SERVICE_NAME} && sudo rm /etc/systemd/system/${SERVICE_NAME}.service"

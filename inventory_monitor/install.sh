#!/usr/bin/env bash
#
# 라즈베리파이 재고 모니터링 설치 스크립트
# 사용법: sudo bash install.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CURRENT_USER="${SUDO_USER:-$USER}"

echo "=== 재고 모니터링 설치 ==="
echo "프로젝트 경로: $PROJECT_DIR"
echo "실행 사용자:   $CURRENT_USER"
echo ""

# 1. 의존성 설치
echo "[1/6] Python 패키지 설치..."
pip3 install --break-system-packages httpx bcrypt 2>/dev/null \
    || pip3 install httpx bcrypt

# 2. credentials.json 확인
if [ ! -f "$PROJECT_DIR/config/credentials.json" ]; then
    echo "ERROR: $PROJECT_DIR/config/credentials.json 파일이 없습니다."
    exit 1
fi

# 3. inventory-monitor 서비스 (재고 수집)
echo "[2/6] inventory-monitor 서비스 등록..."
cat > /etc/systemd/system/inventory-monitor.service <<EOF
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

# 4. inventory-server 서비스 (HTTP API)
echo "[3/6] inventory-server 서비스 등록..."
cat > /etc/systemd/system/inventory-server.service <<EOF
[Unit]
Description=Smart Inventory API Server (port 8765)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=/usr/bin/python3 ${PROJECT_DIR}/inventory_monitor/server.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 5. cloudflared 터널 서비스
echo "[4/6] cloudflared 터널 서비스 등록..."
cat > /etc/systemd/system/inventory-tunnel.service <<EOF
[Unit]
Description=Cloudflare Tunnel for Inventory API
After=network-online.target inventory-server.service
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
ExecStart=/usr/local/bin/cloudflared tunnel --url http://localhost:8765 --no-autoupdate
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 6. 서비스 등록 및 시작
echo "[5/6] 서비스 등록..."
systemctl daemon-reload
systemctl enable inventory-monitor.service inventory-server.service inventory-tunnel.service

echo "[6/6] 서비스 시작..."
systemctl restart inventory-monitor.service
systemctl restart inventory-server.service
systemctl restart inventory-tunnel.service

echo ""
echo "=== 설치 완료! ==="
echo ""
echo "터널 URL 확인:"
echo "  sudo journalctl -u inventory-tunnel -n 30 | grep trycloudflare"
echo ""
echo "유용한 명령어:"
echo "  수집 로그:  sudo journalctl -u inventory-monitor -f"
echo "  서버 로그:  sudo journalctl -u inventory-server -f"
echo "  터널 로그:  sudo journalctl -u inventory-tunnel -f"

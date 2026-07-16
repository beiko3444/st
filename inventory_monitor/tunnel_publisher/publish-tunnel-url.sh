#!/usr/bin/env bash
# cloudflared quick tunnel URL 을 GitHub secret gist 에 업로드.
#
# 사용 환경:
# - Pi 에 cloudflared 가 이미 running (journalctl/ps 에서 URL 추출 가능해야 함)
# - ~/.github_gist_token 에 PAT (gist 스코프) 저장
# - GIST_ID 는 아래 상수로 하드코딩
set -euo pipefail

GIST_ID="5a69e99d96fa2ae34ba4af96c117d5e0"
TOKEN_FILE="${HOME}/.github_gist_token"
LOG_TAG="publish-tunnel-url"

log() { echo "[$(date -Iseconds)] [${LOG_TAG}] $*"; }

if [[ ! -r "${TOKEN_FILE}" ]]; then
    log "ERROR: token file not found: ${TOKEN_FILE}"
    exit 2
fi
TOKEN="$(tr -d '[:space:]' < "${TOKEN_FILE}")"
if [[ -z "${TOKEN}" ]]; then
    log "ERROR: token file empty"
    exit 2
fi

# URL 추출: journalctl 에서 cloudflared 가 뿌린 trycloudflare URL 의 가장 최근 것
# (quick tunnel 은 시작 시 로그에 https://xxx.trycloudflare.com 형태로 한 번 출력됨)
extract_url() {
    # 현 부팅의 inventory tunnel 로그에서 가장 최근 매칭 하나
    journalctl -u inventory-tunnel.service -b --no-pager 2>/dev/null \
        | grep -Eio 'https://[a-z0-9-]+\.trycloudflare\.com' \
        | grep -v '^https://api\.trycloudflare\.com$' \
        | tail -1
}

check_tunnel() {
    curl -sSf --max-time 8 "${1}/status" > /dev/null
}

# cloudflared 가 아직 URL 을 뿌리지 않았을 수 있으므로 최대 60초 폴링
DEADLINE=$(( $(date +%s) + 60 ))
TUNNEL_URL=""
while [[ "$(date +%s)" -lt ${DEADLINE} ]]; do
    TUNNEL_URL="$(extract_url)"
    if [[ -n "${TUNNEL_URL}" ]]; then
        break
    fi
    sleep 3
done

if [[ -z "${TUNNEL_URL}" ]]; then
    log "ERROR: could not extract cloudflared URL within 60s"
    exit 3
fi
log "extracted URL: ${TUNNEL_URL}"

# Pi 자체 health check — URL 이 실제로 응답하는지 확인 (아직 tunnel 이 뜨는 중일 수도 있음)
for attempt in 1 2 3 4 5; do
    if check_tunnel "${TUNNEL_URL}"; then
        log "health check passed on attempt ${attempt}"
        break
    fi
    log "health check failed attempt ${attempt}, retrying..."
    sleep 5
    if [[ ${attempt} -eq 5 ]]; then
        STALE_URL="${TUNNEL_URL}"
        log "tunnel URL is stale; restarting inventory-tunnel.service"
        if ! sudo -n systemctl restart inventory-tunnel.service; then
            log "ERROR: failed to restart inventory-tunnel.service"
            exit 4
        fi

        RESTART_DEADLINE=$(( $(date +%s) + 60 ))
        TUNNEL_URL=""
        while [[ "$(date +%s)" -lt ${RESTART_DEADLINE} ]]; do
            CANDIDATE_URL="$(extract_url)"
            if [[ -n "${CANDIDATE_URL}" && "${CANDIDATE_URL}" != "${STALE_URL}" ]] && check_tunnel "${CANDIDATE_URL}"; then
                TUNNEL_URL="${CANDIDATE_URL}"
                log "replacement tunnel is healthy: ${TUNNEL_URL}"
                break
            fi
            sleep 3
        done

        if [[ -z "${TUNNEL_URL}" ]]; then
            log "ERROR: replacement tunnel did not become healthy within 60s"
            exit 4
        fi
    fi
done

# gist 현재 값 조회해서 같으면 skip (gist API rate limit 절약)
CURRENT_URL=""
CURRENT_URL="$(curl -sS --max-time 10 "https://gist.githubusercontent.com/beiko3444/${GIST_ID}/raw?t=$(date +%s)" \
                | python3 -c 'import json,sys; print(json.load(sys.stdin).get("url",""))' 2>/dev/null || true)"
if [[ "${CURRENT_URL}" == "${TUNNEL_URL}" ]]; then
    log "gist already up-to-date, skip"
    exit 0
fi

# gist 업데이트 (PATCH)
PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'files': {'monitor.json': {'content': json.dumps({'url': sys.argv[1]})}}}))" "${TUNNEL_URL}")
HTTP_CODE=$(curl -sS -o /tmp/gist_response.json -w '%{http_code}' \
    -X PATCH \
    -H "Authorization: token ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    --max-time 20 \
    --data "${PAYLOAD}" \
    "https://api.github.com/gists/${GIST_ID}")

if [[ "${HTTP_CODE}" != "200" ]]; then
    log "ERROR: gist PATCH failed HTTP ${HTTP_CODE}:"
    cat /tmp/gist_response.json >&2 || true
    exit 5
fi
log "gist updated OK (${TUNNEL_URL})"

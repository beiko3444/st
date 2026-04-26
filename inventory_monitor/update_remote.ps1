# 라즈베리파이 inventory monitor/server 코드 업데이트 (Windows PowerShell 버전)
#
# 사용:
#   .\inventory_monitor\update_remote.ps1
#   .\inventory_monitor\update_remote.ps1 -Target beiko@192.168.0.42 -RemoteDir /home/beiko/st
#
# rsync 가 없어 scp 로 폴더 통째 전송. 처음 한 번 host key 자동 등록.

param(
    [string]$Target = "beiko@raspberrypi.local",
    [string]$RemoteDir = "/home/beiko/st"
)

$ErrorActionPreference = "Stop"

Write-Host "사용 대상: $Target"
Write-Host "원격 경로: $RemoteDir"

# 0) 기존 호스트키 제거 (IP 바뀌었을 때 충돌 방지)
$hostOnly = ($Target -split "@")[-1]
Write-Host "[0/4] 기존 host key 제거 시도 ($hostOnly)..."
& ssh-keygen.exe -R $hostOnly 2>$null | Out-Null

# 1) 연결 확인
Write-Host "[1/4] 연결 확인: $Target"
$test = & ssh.exe -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new $Target 'echo connected: $(hostname)'
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "연결 실패입니다." -ForegroundColor Red
    Write-Host "- 라즈베리에서 'whoami' 로 사용자 확인 후 재시도:"
    Write-Host "    .\inventory_monitor\update_remote.ps1 -Target <user>@raspberrypi.local -RemoteDir /home/<user>/st"
    Write-Host "- 또는 IP 직접 지정: -Target beiko@192.168.0.x"
    exit 1
}
Write-Host $test

# 2) 코드 전송 (scp 로 디렉터리 재귀 복사)
$srcDir = Join-Path (Split-Path -Parent $PSCommandPath) "."
Write-Host "[2/4] 코드 전송: $srcDir → ${Target}:${RemoteDir}/inventory_monitor/"
# 원격 폴더 준비
& ssh.exe $Target "mkdir -p $RemoteDir/inventory_monitor"
if ($LASTEXITCODE -ne 0) { Write-Host "원격 폴더 생성 실패" -ForegroundColor Red; exit 1 }

# .py / .sh / .service / .ps1 파일 모두 전송 (__pycache__ 제외)
$files = Get-ChildItem -Path $srcDir -Recurse -File |
    Where-Object { $_.FullName -notmatch "__pycache__" }

foreach ($f in $files) {
    $rel = $f.FullName.Substring($srcDir.Length).TrimStart("\","/").Replace("\","/")
    $remote = "$RemoteDir/inventory_monitor/$rel"
    # 원격 상위 디렉터리 보장
    $remoteDirOnly = $remote.Substring(0, $remote.LastIndexOf("/"))
    & ssh.exe $Target "mkdir -p '$remoteDirOnly'" 2>$null | Out-Null
    & scp.exe -q $f.FullName "${Target}:${remote}"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  전송 실패: $rel" -ForegroundColor Red
        exit 1
    }
}
Write-Host "  전송 완료: $($files.Count) 파일"

# 3) 서비스 재시작
Write-Host "[3/4] 서비스 재시작"
& ssh.exe -tt $Target "sudo systemctl restart inventory-monitor.service inventory-server.service"
if ($LASTEXITCODE -ne 0) {
    Write-Host "서비스 재시작 실패 (sudo 권한 확인)" -ForegroundColor Yellow
}

# 4) 상태 확인
Write-Host "[4/4] 상태 확인"
& ssh.exe -tt $Target "sudo systemctl --no-pager --full status inventory-monitor.service inventory-server.service | sed -n '1,40p'"

Write-Host ""
Write-Host "완료: 라즈베리 코드 반영 + 서비스 재시작" -ForegroundColor Green

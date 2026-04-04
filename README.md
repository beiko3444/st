# SmartStore + 쿠팡로켓 재고 조회 GUI

윈도우에서 실행하는 Python(PySide6) 기반 데스크톱 앱입니다.

아래 컬럼을 통합 조회합니다.

- 연번
- 상품이미지
- 상품명
- 재고
- 가격
- 채널
- 마지막 동기화 시각

## 기능

- 스마트스토어 `POST /v1/products/search` 조회
- 쿠팡 로켓그로스 상품 목록 + 상세 + RG Inventory 조회
- 채널 필터(전체/스마트스토어/쿠팡로켓)
- 상품명 검색
- 이미지 비동기 로딩

## 프로젝트 구조

- `main.py`: 앱 진입점
- `config/credentials.json`: API 키 설정
- `inventory_app/config.py`: 설정 로더
- `inventory_app/connectors/smartstore.py`: 스마트스토어 커넥터
- `inventory_app/connectors/coupang.py`: 쿠팡 커넥터
- `inventory_app/services/aggregator.py`: 채널 통합 서비스
- `inventory_app/ui/main_window.py`: 메인 GUI

## 실행 방법 (Windows PowerShell)

1. Python 3.11+ 설치
2. 프로젝트 폴더에서 가상환경 생성/활성화

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

3. 패키지 설치

```powershell
pip install -r requirements.txt
```

4. 실행

```powershell
python main.py
```

## EXE 빌드/실행

이미 빌드가 완료된 경우 실행 파일 경로:

- `dist\\SmartInventory\\SmartInventory.exe`

재빌드:

```powershell
.\build_exe.ps1
```

## 참고

- 현재 `config/credentials.json`에 전달받은 키가 그대로 반영되어 있습니다.
- 키가 갱신되면 해당 파일 값만 바꾸면 됩니다.

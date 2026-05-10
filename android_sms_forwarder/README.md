# SmartInventory Notification Forwarder

개인 APK로 설치해서 문자앱 알림 중 지정 키워드가 들어간 내용만 라즈베리 DB에 저장하는 앱입니다.

## 동작 방식

- SMS 직접 수신 권한은 사용하지 않습니다. Android의 악성 앱 차단을 피하기 위해 알림 접근(Notification Listener) 방식으로 문자앱 알림을 읽습니다.
- 앱 설정의 키워드 목록 중 하나라도 알림 본문에 포함되면 `POST /sms-messages`로 전송합니다.
- 서버 URL, API 토큰, 키워드, 전송 ON/OFF는 앱 화면에서 설정합니다.
- 네트워크 실패 시 WorkManager가 연결 복구 후 재전송합니다.
- 같은 알림은 `msg_key = sms-notification|<device_id>|<notification_key>` 기준으로 라즈베리 DB에서 중복 방지됩니다.

## 라즈베리 서버 토큰

라즈베리 서버는 아래 중 하나로 토큰을 읽습니다.

1. 환경변수 `SMARTINVENTORY_SMS_API_TOKEN`
2. `config/credentials.json`의 `monitor.sms_api_token`
3. `config/credentials.json`의 `sms_api_token`

토큰이 설정되어 있으면 `POST /sms-messages` 요청에 아래 헤더 중 하나가 필요합니다.

```http
Authorization: Bearer <token>
X-Api-Token: <token>
```

토큰이 비어 있으면 기존 호환성을 위해 SMS POST 인증을 강제하지 않습니다.

## APK 빌드

```bash
cd android_sms_forwarder
gradle :app:assembleDebug
```

빌드 결과:

```text
app/build/outputs/apk/debug/app-debug.apk
```

## 앱 설정

- 서버 URL: 라즈베리 터널 또는 도메인 URL, 예: `https://xxxxx.trycloudflare.com`
- API 토큰: 라즈베리 서버에 설정한 토큰
- 키워드: `승인, 입금, 쿠팡`처럼 콤마 또는 줄바꿈으로 입력
- 알림 접근 허용: Android 설정 화면에서 `SMS 포워더`의 알림 접근을 허용
- 전송 테스트: 실제 테스트 SMS 레코드를 라즈베리 DB에 저장해서 토큰/URL을 검증

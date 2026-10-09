# KR 즉시 번역기

한국어를 붙여넣으면 **영어**와 **중국어 간체**로 바로 번역해 실시간(스트리밍)으로 보여주는 로컬 웹 도구입니다.

- **번역 = GPT**: 공식 Codex CLI의 `codex app-server`를 통해 **ChatGPT 계정으로 로그인**하고, 그 구독(Codex가 포함된 플랜)의 사용량으로 번역합니다. OpenAI API 키는 필요 없습니다.
- **문체 판단 = Jev (TypeSafe AI, 선택)**: 붙여넣은 글이 어떤 종류인지(거래처 업무 / 상품·광고 / 고객응대 / 일상대화 / 기술·사양) 판단해 GPT에게 맞는 번역 문체를 지시합니다.

```
붙여넣기 ─▶ Jev: 문체 판단 (최대 1.5초, 실패해도 번역은 진행)
          ├▶ GPT 영어 번역  ─┐  두 언어를 동시에 요청하고
          └▶ GPT 중국어 번역 ─┴▶ 글자가 오는 대로 화면에 표시
```

## Jev는 번역을 하지 못합니다

Jev는 2026-09-15 TypeSafe AI가 공개한 "System One" 모델로, **텍스트를 생성하지 않는 판단 전용 모델**입니다.
미리 정한 질문(선택지 / 점수 / 예·아니오)에 확률과 함께 답할 뿐 문장을 만들지 않으므로 번역 자체는 할 수 없습니다.
그래서 이 도구에서는 번역은 GPT가 하고, Jev는 번역 전에 문체를 고르는 역할만 맡습니다.

- 신뢰도가 `JEV_MIN_CONFIDENCE`(기본 0.5) 미만이거나 "기타"로 판단하면 기본 문체로 번역합니다.
- Jev가 `JEV_TIMEOUT`(기본 1.5초) 안에 답하지 않으면 건너뛰고 바로 번역합니다.
- 문체를 직접 고르면 Jev를 부르지 않습니다.
- Jev의 한국어 판단 정확도에 대한 공개 자료는 찾지 못했습니다. 화면의 `Jev: 업무·거래처 91% · 140ms` 칩으로 판단 결과와 걸린 시간을 확인할 수 있습니다.

## 준비

1. Python 패키지 (저장소 루트에서)

   ```bash
   pip install -r requirements.txt
   ```

2. Codex CLI (Node.js 필요)

   ```bash
   npm install -g @openai/codex
   ```

3. 실행

   ```bash
   python3 -m translator --open
   ```

   브라우저에서 `http://127.0.0.1:8777`이 열립니다. **브라우저로 로그인**을 누르고 ChatGPT 계정으로 로그인하면 바로 사용할 수 있습니다.
   터미널에서 이미 `codex login`을 했다면 같은 로그인(`~/.codex/auth.json`)을 그대로 씁니다.
   다른 기기(예: 라즈베리파이)에서 서버를 돌린다면 **기기 코드로 로그인**을 쓰세요.

## Jev 설정 (선택)

둘 중 하나만 설정하면 됩니다. 아무것도 없으면 Jev 없이 기본 문체로 번역합니다.

| 방법 | 환경변수 |
| --- | --- |
| TypeSafe API (현재 early access 대기자 명단) | `TYPESAFE_API_KEY` |
| Cloudflare Workers AI (`typesafe/jev`) | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` (Workers AI 권한) |

```bash
export TYPESAFE_API_KEY="..."
python3 -m translator --open
```

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `JEV_PROVIDER` | `auto` | `auto` / `typesafe` / `cloudflare` / `off` |
| `JEV_TIMEOUT` | `1.5` | Jev 응답을 기다리는 최대 초 |
| `JEV_MIN_CONFIDENCE` | `0.5` | 이 값 이상일 때만 Jev 판단을 적용 |
| `JEV_MODEL` | `jev-latest` / `typesafe/jev` | 모델 이름 |
| `TYPESAFE_BASE_URL` | `https://api.typesafe.ai` | TypeSafe API 주소 |

## 실행 옵션

| 옵션 | 환경변수 | 설명 |
| --- | --- | --- |
| `--port` | `TRANSLATOR_PORT` | 기본 8777 |
| `--host` | `TRANSLATOR_HOST` | 기본 127.0.0.1 (아래 보안 참고) |
| `--model` | `TRANSLATOR_MODEL` | 번역 모델. 기본은 Codex 기본 모델 |
| `--effort` | `TRANSLATOR_EFFORT` | 추론 강도. 기본은 모델이 지원하는 값 중 `none` → `minimal` → `low` 순으로 가장 빠른 것 |
| `--prompt-mode` | `TRANSLATOR_PROMPT_MODE` | `base`(기본) 또는 `developer` |
| `--codex-bin` | `TRANSLATOR_CODEX_BIN` | `codex` 실행 파일 경로 |
| | `TRANSLATOR_CODEX_ARGS` | `codex app-server`에 덧붙일 인자 |

## 사용법

- **붙여넣기(Ctrl/⌘+V)** 하는 순간 번역을 시작합니다.
- 직접 입력하면 0.7초 멈췄을 때 번역합니다(`입력 중 자동 번역` 체크 해제 시 끔). **Ctrl/⌘+Enter**는 즉시 번역.
- 번역 중에 새로 붙여넣으면 이전 번역을 중단(`turn/interrupt`)하고 새 글을 번역합니다.
- 같은 글을 다시 번역하면 메모리 캐시에서 바로 보여주고 구독 사용량을 쓰지 않습니다.
- 각 결과의 **복사** 버튼으로 바로 복사합니다. 하단에 첫 글자까지 걸린 시간과 완료 시간이 표시됩니다.

## 동작 방식과 안전장치

- 언어마다 별도의 일회성(ephemeral) Codex 스레드를 열어 영어·중국어를 **동시에** 스트리밍합니다. 그래서 한 번 번역할 때 요청이 2건 나갑니다.
- Codex 기본 코딩 에이전트 프롬프트(약 17KB) 대신 번역 전용 지시문(약 1KB)을 `baseInstructions`로 보내 첫 글자가 빨리 나오게 합니다.
  ChatGPT 서버가 이를 거부하면 자동으로 `developerInstructions` 방식으로 바꿔 다시 시도합니다.
- 셸·웹검색·플러그인 등 도구를 끄고(`-c features.*=false`), 읽기 전용 샌드박스·승인 없음·빈 작업 폴더로 실행합니다.
  붙여넣은 글 안에 "명령"이 있어도 실행되지 않고 그대로 번역됩니다.
- 서버는 기본적으로 `127.0.0.1`에서만 열리고, 다른 사이트에서 보낸 요청과 다른 호스트 이름으로 들어온 요청(DNS rebinding)은 막습니다.
  `--host 0.0.0.0`으로 열면 같은 네트워크의 누구나 내 구독으로 번역할 수 있으니 주의하세요.

## 알아둘 점

- **사용량**: 번역은 ChatGPT 플랜의 Codex 사용량 한도에서 차감됩니다. 상단에 Codex가 알려주는 한도별 사용률(예: `5시간 12% / 7일 3%`)이 표시됩니다.
  입력 중 자동 번역은 요청 수를 늘리므로, 아끼려면 끄고 붙여넣기/Ctrl+Enter만 쓰세요.
- **API 키 로그인 주의**: Codex에 API 키로 로그인되어 있으면 구독이 아니라 API 요금이 청구됩니다. 이때 화면 상단에 경고가 표시됩니다.
- OpenAI 문서는 CI 같은 자동화에는 API 키 사용을 권장합니다. 이 도구는 본인이 직접 쓰는 로컬 도구로 만들었습니다.
- 확인한 버전: Codex CLI 0.162.0 (2026-10-09 npm 최신). `codex app-server` 프로토콜은 Codex 쪽에서 실험 기능으로 표시되어 있어, 이후 버전에서 바뀔 수 있습니다.
- Jev 요청 형식은 TypeSafe 공식 SDK(`@typesafe-ai/sdk` 0.6.0) 기준입니다. Cloudflare 응답은 `{"result": ...}` 포장이 있든 없든 처리합니다.

## 테스트

```bash
python3 -m unittest tests.test_translator
```

실제 Codex 실행 파일로 확인하려면(모델 서버만 가짜로 대체):

```bash
CODEX_BIN="$(which codex)" python3 -m unittest tests.test_translator.RealCodexIntegrationTests
```

## 참고 자료

- Codex 인증(ChatGPT 로그인 / API 키): https://developers.openai.com/codex/auth
- Codex CLI npm 패키지: https://www.npmjs.com/package/@openai/codex
- Jev 소개(TypeSafe AI): https://typesafe.ai/blog/introducing-system-one-models-and-jev
- TypeSafe JavaScript SDK: https://www.npmjs.com/package/@typesafe-ai/sdk
- Cloudflare Workers AI의 Jev: https://developers.cloudflare.com/ai/models/typesafe/jev/
- Cloudflare Workers AI 모델 실행 API: https://developers.cloudflare.com/api/resources/ai/methods/run/

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
import threading
import webbrowser

import uvicorn

from .app import LOOPBACK_HOSTS, create_app
from .service import PROMPT_MODES, TranslationService


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="한국어를 붙여넣으면 영어·중국어 간체로 바로 번역 (ChatGPT 로그인 + Jev)"
    )
    parser.add_argument("--host", default=os.environ.get("TRANSLATOR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("TRANSLATOR_PORT", "8777")))
    parser.add_argument("--open", action="store_true", help="브라우저에서 번역기 열기")
    parser.add_argument("--codex-bin", help="codex 실행 파일 경로 (기본: PATH의 codex)")
    parser.add_argument("--model", help="번역에 쓸 모델 (기본: Codex 기본 모델)")
    parser.add_argument("--effort", help="추론 강도 (기본: 모델이 지원하는 가장 빠른 값)")
    parser.add_argument("--prompt-mode", choices=PROMPT_MODES, help="번역 지시문 전달 방식")
    args = parser.parse_args()

    overrides = {
        "TRANSLATOR_CODEX_BIN": args.codex_bin,
        "TRANSLATOR_MODEL": args.model,
        "TRANSLATOR_EFFORT": args.effort,
        "TRANSLATOR_PROMPT_MODE": args.prompt_mode,
    }
    for key, value in overrides.items():
        if value:
            os.environ[key] = value

    service = TranslationService.from_env()
    if service.codex is None:
        print(f"[translator] {service.missing_codex_hint}", file=sys.stderr)
    if service.jev is None:
        print("[translator] Jev 미설정: 스타일 자동 판단 없이 번역합니다 (translator/README.md 참고).", file=sys.stderr)

    allowed_hosts = None
    if not _is_loopback(args.host):
        print(
            "[translator] 경고: 루프백이 아닌 주소로 열면 같은 네트워크의 누구나 "
            "내 ChatGPT 구독으로 번역할 수 있습니다.",
            file=sys.stderr,
        )
        allowed_hosts = ["*"]
    app = create_app(service, allowed_hosts=allowed_hosts or LOOPBACK_HOSTS)

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"[translator] {url}", file=sys.stderr)
    if args.open:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

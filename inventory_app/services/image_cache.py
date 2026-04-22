"""영구 이미지 디스크 캐시.

HTTP 이미지 다운로드 결과를 ~/.smartinventory/image_cache/ 에 저장.
- 앱을 껐다 켜도 재다운로드 하지 않음 → 시작 시 팬 소음/네트워크 부하 제거
- TTL: 기본 30일 (이후 자동 재다운로드로 최신화)
- 정책: URL → SHA1 → 16자 hex prefix/suffix 2단 디렉토리 구조 (파일 수 분산)
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

import httpx

__all__ = [
    "get_image_bytes",
    "clear_disk_cache",
    "disk_cache_root",
]

_GUARD = threading.Lock()
_DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def disk_cache_root() -> Path:
    from_env = os.environ.get("SMARTINVENTORY_IMAGE_CACHE_DIR", "").strip()
    if from_env:
        root = Path(from_env).expanduser().resolve()
    else:
        root = (Path.home() / ".smartinventory" / "image_cache").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _hash_url(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def _cache_path(url: str) -> Path:
    digest = _hash_url(url)
    # 2단 디렉토리: aa/bb/aabbcc... 한 폴더에 파일이 수만 개 쌓이지 않도록.
    root = disk_cache_root()
    sub = root / digest[:2] / digest[2:4]
    sub.mkdir(parents=True, exist_ok=True)
    return sub / digest


def _load_from_disk(path: Path, ttl_seconds: int) -> bytes | None:
    try:
        stat = path.stat()
    except (FileNotFoundError, OSError):
        return None
    if ttl_seconds > 0 and (time.time() - stat.st_mtime) > ttl_seconds:
        # TTL 지난 캐시는 무효화
        try:
            path.unlink()
        except OSError:
            pass
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _save_to_disk(path: Path, data: bytes) -> None:
    # 쓰기 도중 파일이 읽히는 것을 막기 위해 .tmp 로 쓰고 rename
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
        except Exception:  # noqa: BLE001
            pass


def _download_via_http(url: str, timeout: int) -> bytes | None:
    try:
        response = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers=_DEFAULT_HEADERS,
        )
        if response.status_code != 200:
            return None
        content_type = (response.headers.get("content-type") or "").lower()
        if "image" not in content_type:
            return None
        return response.content
    except Exception:  # noqa: BLE001
        return None


def get_image_bytes(
    url: str,
    *,
    timeout: int = 15,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> bytes | None:
    """디스크 캐시 우선. 없거나 TTL 지났으면 HTTP 다운로드 → 디스크 저장.

    - 같은 URL 동시 호출이 있어도 파일 I/O는 race에 관대 (rename 원자성)
    - 실패 시 None 반환 (기존 _download_image_bytes와 호환)
    """
    if not url:
        return None

    path = _cache_path(url)
    cached = _load_from_disk(path, ttl_seconds)
    if cached is not None:
        return cached

    data = _download_via_http(url, timeout)
    if data:
        # guard 로 파일쓰기 경합 최소화 (프로세스 내)
        with _GUARD:
            _save_to_disk(path, data)
    return data


def clear_disk_cache() -> int:
    """캐시 전체 삭제. 반환값: 삭제된 파일 수."""
    root = disk_cache_root()
    removed = 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed

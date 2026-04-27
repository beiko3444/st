"""쿠팡 자동로그인용 다중 자격증명 저장소.

- 1순위: 라즈베리파이 DB (`/coupang-credentials`)
- 2순위(오프라인 fallback): `~/.smartinventory/coupang_logins.json` (0600)

저장 형식: list[{label, email, password (평문), updated_at}]
Pi/네트워크에는 password_obf (base64) 로 전송.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


@dataclass
class CoupangAccount:
    label: str
    email: str
    password: str
    updated_at: str = ""


def _store_path() -> Path:
    return Path.home() / ".smartinventory" / "coupang_logins.json"


def _legacy_single_path() -> Path:
    return Path.home() / ".smartinventory" / "coupang_login.json"


def _obfuscate(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _deobfuscate(value: str) -> str:
    try:
        return base64.b64decode(value.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


# ----- 로컬 캐시 -----

def _load_local() -> List[CoupangAccount]:
    path = _store_path()
    if not path.exists():
        # 레거시(단일 계정) 마이그레이션
        legacy = _legacy_single_path()
        if legacy.exists():
            try:
                data = json.loads(legacy.read_text(encoding="utf-8"))
                email = str(data.get("email") or "").strip()
                pw = _deobfuscate(str(data.get("password") or ""))
                if email and pw:
                    acc = CoupangAccount(label=email, email=email, password=pw)
                    _save_local([acc])
                    return [acc]
            except Exception:
                pass
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    accounts: List[CoupangAccount] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        email = str(entry.get("email") or "").strip()
        pw_obf = str(entry.get("password_obf") or "")
        password = _deobfuscate(pw_obf) if pw_obf else str(entry.get("password") or "")
        updated_at = str(entry.get("updated_at") or "")
        if not label or not email or not password:
            continue
        accounts.append(CoupangAccount(label=label, email=email, password=password, updated_at=updated_at))
    return accounts


def _save_local(accounts: List[CoupangAccount]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "label": a.label,
            "email": a.email,
            "password_obf": _obfuscate(a.password),
            "updated_at": a.updated_at,
        }
        for a in accounts
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


# ----- 공개 API -----

def list_accounts(pi_client=None) -> List[CoupangAccount]:
    """저장된 모든 계정 목록. Pi 우선, 실패 시 로컬."""
    if pi_client is not None and getattr(pi_client, "is_configured", False):
        try:
            rows = pi_client.list_coupang_credentials()
            if rows:
                accounts: List[CoupangAccount] = []
                for r in rows:
                    label = str(r.get("label") or "").strip()
                    email = str(r.get("email") or "").strip()
                    pw_obf = str(r.get("password_obf") or "")
                    password = _deobfuscate(pw_obf)
                    if not label or not email or not password:
                        continue
                    accounts.append(CoupangAccount(
                        label=label, email=email, password=password,
                        updated_at=str(r.get("updated_at") or ""),
                    ))
                # Pi 데이터로 로컬 캐시 동기화
                _save_local(accounts)
                return accounts
        except Exception:
            pass
    return _load_local()


def save_account(label: str, email: str, password: str, pi_client=None) -> None:
    """단일 계정 저장 (Pi + 로컬 캐시 모두)."""
    label = label.strip()
    email = email.strip()
    if not label or not email or not password:
        raise ValueError("계정명/이메일/비밀번호 모두 필요합니다.")
    pushed = False
    if pi_client is not None and getattr(pi_client, "is_configured", False):
        try:
            pi_client.save_coupang_credential(label, email, _obfuscate(password))
            pushed = True
        except Exception:
            pushed = False
    # 로컬 캐시 갱신
    accounts = _load_local()
    accounts = [a for a in accounts if a.label != label]
    from datetime import datetime as _dt
    accounts.insert(0, CoupangAccount(
        label=label, email=email, password=password,
        updated_at=_dt.now().isoformat(),
    ))
    _save_local(accounts)
    if not pushed and pi_client is not None and getattr(pi_client, "is_configured", False):
        # Pi 통신 실패 알림은 호출부에서 결정
        raise RuntimeError("Pi 저장 실패 — 로컬에만 저장됨")


def delete_account(label: str, pi_client=None) -> None:
    label = label.strip()
    if not label:
        return
    if pi_client is not None and getattr(pi_client, "is_configured", False):
        try:
            pi_client.delete_coupang_credential(label)
        except Exception:
            pass
    accounts = [a for a in _load_local() if a.label != label]
    _save_local(accounts)


def get_account(label: str, pi_client=None) -> Optional[CoupangAccount]:
    for a in list_accounts(pi_client=pi_client):
        if a.label == label:
            return a
    return None


# ----- 레거시 호환 (단일 계정 API) -----

def load_credentials(pi_client=None) -> Tuple[str, str]:
    """가장 최근 계정 반환 (없으면 빈 문자열)."""
    accounts = list_accounts(pi_client=pi_client)
    if not accounts:
        return "", ""
    a = accounts[0]
    return a.email, a.password


def save_credentials(email: str, password: str, pi_client=None) -> None:
    """이메일 자체를 라벨로 사용 (레거시 호환)."""
    save_account(email, email, password, pi_client=pi_client)


def clear_credentials(pi_client=None) -> None:
    accounts = _load_local()
    for a in accounts:
        delete_account(a.label, pi_client=pi_client)

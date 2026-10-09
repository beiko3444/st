"""Jev (TypeSafe AI) style classifier.

Jev is a non-generative "System One" model: it answers typed questions
(choice / score / yes-no) about a piece of state with calibrated probabilities,
but it cannot write text, so it cannot translate. Here it reads the pasted
Korean text and picks the writing style GPT should translate in.

Providers:
- TypeSafe API: `POST https://api.typesafe.ai/v1/systemone` with
  `Authorization: Bearer $TYPESAFE_API_KEY` (shape taken from the official
  `@typesafe-ai/sdk` 0.6.0 package).
- Cloudflare Workers AI: `POST /client/v4/accounts/{account_id}/ai/run` with
  `{"model": "typesafe/jev", "input": {...}}`.

Both answer `answers.<question>.{choice, confidence, probabilities}`; the
Cloudflare response is wrapped in the usual `{"result": ...}` envelope.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import httpx

from .prompts import JEV_OTHER_LABEL, jev_style_criteria

TYPESAFE_BASE_URL = "https://api.typesafe.ai"
TYPESAFE_DEFAULT_MODEL = "jev-latest"
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
CLOUDFLARE_DEFAULT_MODEL = "typesafe/jev"

QUESTION_NAME = "style"
QUESTION_INSTRUCTIONS = (
    "The state is Korean text a user pasted into a translator. "
    "Which kind of text is it, so the translation can use the right tone?"
)


class JevError(RuntimeError):
    pass


@dataclass
class JevDecision:
    label: str
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)
    latency_ms: int = 0
    provider: str = ""
    model: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JevClassifier:
    def __init__(
        self,
        provider: str,
        *,
        api_key: str,
        account_id: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 1.5,
        min_confidence: float = 0.5,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        if provider not in ("typesafe", "cloudflare"):
            raise ValueError(f"unknown Jev provider: {provider}")
        if provider == "cloudflare" and not account_id:
            raise ValueError("Cloudflare Workers AI needs an account id")
        self.provider = provider
        self.api_key = api_key
        self.account_id = account_id
        self.base_url = (base_url or (TYPESAFE_BASE_URL if provider == "typesafe" else CLOUDFLARE_API_BASE)).rstrip("/")
        self.model = model or (TYPESAFE_DEFAULT_MODEL if provider == "typesafe" else CLOUDFLARE_DEFAULT_MODEL)
        self.timeout = timeout
        self.min_confidence = min_confidence
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> Optional["JevClassifier"]:
        """Build from environment variables; `None` when Jev is off or not configured."""
        env = dict(os.environ if env is None else env)
        choice = (env.get("JEV_PROVIDER") or "auto").strip().lower()
        if choice == "off":
            return None
        common = {
            "model": env.get("JEV_MODEL") or None,
            "timeout": float(env.get("JEV_TIMEOUT") or 1.5),
            "min_confidence": float(env.get("JEV_MIN_CONFIDENCE") or 0.5),
        }
        typesafe_key = env.get("TYPESAFE_API_KEY", "").strip()
        cf_account = env.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
        cf_token = env.get("CLOUDFLARE_API_TOKEN", "").strip()
        if choice in ("auto", "typesafe") and typesafe_key:
            return cls("typesafe", api_key=typesafe_key, base_url=env.get("TYPESAFE_BASE_URL") or None, **common)
        if choice in ("auto", "cloudflare") and cf_account and cf_token:
            return cls("cloudflare", api_key=cf_token, account_id=cf_account, **common)
        return None

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "timeoutSec": self.timeout,
            "minConfidence": self.min_confidence,
        }

    async def close(self) -> None:
        await self._client.aclose()

    def build_payload(self, text: str) -> dict[str, Any]:
        questions = {
            QUESTION_NAME: {
                "type": "choice",
                "instructions": QUESTION_INSTRUCTIONS,
                "criteria": jev_style_criteria(),
            }
        }
        if self.provider == "typesafe":
            return {"model": self.model, "state": text, "questions": questions}
        return {"model": self.model, "input": {"state": text, "questions": questions}}

    def endpoint(self) -> str:
        if self.provider == "typesafe":
            return f"{self.base_url}/v1/systemone"
        return f"{self.base_url}/accounts/{self.account_id}/ai/run"

    async def classify(self, text: str) -> JevDecision:
        started = time.perf_counter()
        try:
            response = await self._client.post(
                self.endpoint(),
                json=self.build_payload(text),
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except httpx.TimeoutException as exc:
            raise JevError(f"timeout after {self.timeout:g}s") from exc
        except httpx.HTTPError as exc:
            raise JevError(f"request failed: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            raise JevError(f"HTTP {response.status_code}: {response.text[:300]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise JevError("response is not JSON") from exc
        answer = _find_answer(data)
        label = answer.get("choice")
        if not isinstance(label, str):
            raise JevError(f"unexpected response: {str(data)[:300]}")
        probabilities = {
            str(key): float(value)
            for key, value in (answer.get("probabilities") or {}).items()
            if isinstance(value, (int, float))
        }
        confidence = answer.get("confidence")
        if not isinstance(confidence, (int, float)):
            confidence = probabilities.get(label, 0.0)
        model = data.get("model") if isinstance(data, dict) else None
        return JevDecision(
            label=label,
            confidence=float(confidence),
            probabilities=probabilities,
            latency_ms=latency_ms,
            provider=self.provider,
            model=model or self.model,
        )

    def accepts(self, decision: JevDecision) -> bool:
        return decision.label != JEV_OTHER_LABEL and decision.confidence >= self.min_confidence


def _find_answer(data: Any) -> dict[str, Any]:
    # TypeSafe: {"answers": {...}}; Cloudflare: {"result": {"answers": {...}}}.
    for _ in range(3):
        if not isinstance(data, dict):
            break
        answers = data.get("answers")
        if isinstance(answers, dict) and isinstance(answers.get(QUESTION_NAME), dict):
            return answers[QUESTION_NAME]
        data = data.get("result")
    return {}

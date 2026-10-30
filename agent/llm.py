"""Model backends: local Ollama, or any OpenAI-compatible endpoint (B5).

``Executor`` and ``Verifier`` depend only on the ``LLM`` protocol
(agent/executor.py), so the backend is configuration, never code (PRD §2.3).
``make_llm(role)`` builds the executor's or the verifier's model from
``MODEL_BACKEND``:

* ``ollama`` (default, the development laptop): exactly what the benchmark has
  always used. ``OllamaLLM()`` reads ``OLLAMA_MODEL``/``OLLAMA_URL``, and the
  verifier is chosen by ``verify.gate.select_verifier_model``, which refuses a
  model of the executor's family.
* ``openai``: ``OpenAICompatLLM`` for vLLM, Ollama's own ``/v1`` endpoint or a
  hosted provider, configured per role (``EXECUTOR_*``/``VERIFIER_*``).

**Rule 3.** An Ollama tag names its family (``llama3:8b`` -> ``llama3``). An
OpenAI-style model id does not: ``meta-llama/Llama-3.1-8B-Instruct`` and
``...-70B-Instruct`` are one family under two names, and comparing the names
would call them independent. So every OpenAI-compatible model must *declare*
its family, independence is judged on the declared families, and production
refuses to start when the two are equal (orchestrator/settings.py).

The backend is a recorded experimental condition (``bench/conditions.py``):
runs on different backends are never compared.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

from agent.executor import (
    LLM,
    MODEL_UNAVAILABLE_STATUS,
    ExecutorError,
    ModelUnavailable,
    OllamaLLM,
)
from erp.resilience import parse_retry_after, retry_call

BACKENDS = ("ollama", "openai")


class TokenBucket:
    """Per-process request rate limit: ``rate_per_min`` with a burst of ``burst``.

    Blocks the caller until a token is free, so a provider's rate limit is met
    by waiting here rather than by collecting 429s. ``rate_per_min=0`` is off.
    """

    def __init__(
        self,
        rate_per_min: int,
        burst: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate_per_s = rate_per_min / 60.0
        self.capacity = float(burst or max(1, rate_per_min // 6))
        self.tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._at = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self.rate_per_s <= 0:
            return
        while True:
            with self._lock:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self._at) * self.rate_per_s)
                self._at = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate_per_s
            self._sleep(wait)


#: One concurrency cap and one rate limiter per endpoint, shared by every model
#: object in the process that talks to it.
_LIMITS: dict[str, tuple[threading.BoundedSemaphore, TokenBucket]] = {}
_LIMITS_LOCK = threading.Lock()


def _limits_for(endpoint: str, concurrency: int, rate_per_min: int) -> tuple[Any, TokenBucket]:
    with _LIMITS_LOCK:
        if endpoint not in _LIMITS:
            _LIMITS[endpoint] = (threading.BoundedSemaphore(concurrency), TokenBucket(rate_per_min))
        return _LIMITS[endpoint]


class OpenAICompatLLM:
    """A model behind ``/v1/chat/completions`` (vLLM, Ollama ``/v1``, hosted APIs)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        family: str,
        api_key: str | None = None,
        timeout: int = 180,
        max_tokens: int = 220,
        temperature: float = 0.0,
        seed: int = 7,
        json_mode: bool = True,
        max_concurrency: int = 2,
        rate_limit_per_min: int = 0,
        attempts: int = 3,
    ) -> None:
        if not family or not family.strip():
            raise ValueError(
                f"{model!r}: an OpenAI-compatible model must declare its family (Rule 3)"
            )
        base = base_url.rstrip("/")
        self.endpoint = (
            f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"
        )
        self.name = model
        self.family = family.strip().lower()
        self._api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.seed = seed
        self.json_mode = json_mode
        self.attempts = attempts
        self._sleep = time.sleep
        self._slots, self._bucket = _limits_for(self.endpoint, max_concurrency, rate_limit_per_min)

    def __repr__(self) -> str:  # never the key
        return (
            f"OpenAICompatLLM(model={self.name!r}, family={self.family!r}, "
            f"endpoint={self.endpoint!r})"
        )

    def complete(self, system: str, user: str) -> str:
        return retry_call(
            lambda: self._complete_once(system, user), attempts=self.attempts, sleep=self._sleep
        )

    def _complete_once(self, system: str, user: str) -> str:
        body: dict[str, Any] = {
            "model": self.name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "seed": self.seed,
            "max_tokens": self.max_tokens,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

        self._bucket.acquire()
        with self._slots:
            try:
                r = requests.post(self.endpoint, json=body, headers=headers, timeout=self.timeout)
            except requests.Timeout as e:
                raise ModelUnavailable(
                    f"model endpoint unreachable at {self.endpoint}: {e}", retryable=False
                ) from e
            except requests.RequestException as e:
                raise ModelUnavailable(f"model endpoint unreachable at {self.endpoint}: {e}") from e

        if r.status_code in (401, 403):
            # A configuration fault, not an answer and not transient: never fed
            # back into the prompt, never retried at once.
            raise ModelUnavailable(
                f"model endpoint refused credentials ({r.status_code})", retryable=False
            )
        if r.status_code in MODEL_UNAVAILABLE_STATUS:
            raise ModelUnavailable(
                f"model endpoint unreachable at {self.endpoint}: {r.status_code} {r.text[:300]}",
                retry_after=parse_retry_after(r.headers.get("Retry-After")),
            )
        if not r.ok:
            raise ExecutorError(f"model endpoint {r.status_code}: {r.text[:300]}")
        try:
            text = r.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ExecutorError(f"model endpoint returned no completion: {r.text[:300]}") from e
        if not text:
            raise ExecutorError("model endpoint returned an empty completion")
        return str(text)


def model_backend() -> str:
    backend = os.environ.get("MODEL_BACKEND", "ollama").strip().lower()
    if backend not in BACKENDS:
        raise ValueError(f"MODEL_BACKEND={backend!r}; expected one of {BACKENDS}")
    return backend


def family_of(llm: LLM) -> str | None:
    """The model's declared family, if its backend declares one."""
    family = getattr(llm, "family", None)
    return str(family) if family else None


def make_llm(role: str, executor_model: str | None = None) -> LLM:
    """The ``executor`` or ``verifier`` model for the configured backend."""
    if role not in ("executor", "verifier"):
        raise ValueError(f"unknown model role {role!r}")
    if model_backend() == "ollama":
        if role == "executor":
            return OllamaLLM()
        from verify.gate import select_verifier_model

        if executor_model is None:
            raise ValueError("choosing an Ollama verifier needs the executor's model")
        return OllamaLLM(model=select_verifier_model(executor_model))

    from orchestrator.settings import load_settings

    s = load_settings()
    base_url = getattr(s, f"{role}_base_url")
    model = getattr(s, f"{role}_openai_model")
    family = getattr(s, f"{role}_family")
    key = getattr(s, f"{role}_api_key")
    if not (base_url and model and family):
        raise ValueError(
            f"MODEL_BACKEND=openai needs {role.upper()}_BASE_URL, {role.upper()}_OPENAI_MODEL "
            f"and {role.upper()}_FAMILY"
        )
    return OpenAICompatLLM(
        base_url=base_url,
        model=model,
        family=family,
        api_key=key.get_secret_value() if key else None,
        timeout=s.model_timeout_s,
        max_concurrency=s.model_max_concurrency,
        rate_limit_per_min=s.model_rate_limit_per_min,
    )

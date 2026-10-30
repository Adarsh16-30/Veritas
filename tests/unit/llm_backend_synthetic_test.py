"""The OpenAI-compatible backend: request shape, typed outages, limits, Rule 3.

Synthetic: ``requests.post`` is replaced by a scripted responder, so no model
server is contacted. What is under test is the client's policy and the
independence check on declared families.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
import requests

from agent.executor import ExecutorError, ModelUnavailable, OllamaLLM
from agent.llm import OpenAICompatLLM, TokenBucket, family_of, make_llm, model_backend
from verify.verifier import Verifier, VerifierError

_ids = itertools.count()


def _endpoint() -> str:
    """A fresh endpoint per test, so the per-endpoint limits never leak between tests."""
    return f"http://model-{next(_ids)}:8000"


class _Resp:
    def __init__(
        self, status: int, body: Any = None, headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status
        self.ok = status < 400
        self.headers = headers or {}
        self._body = (
            body
            if body is not None
            else {"choices": [{"message": {"content": '{"action": "hold"}'}}]}
        )
        self.text = str(self._body)

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _llm(
    monkeypatch: pytest.MonkeyPatch, script: list[Any], **kw: Any
) -> tuple[OpenAICompatLLM, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    def post(url: str, json: dict[str, Any], headers: dict[str, str], timeout: int) -> _Resp:
        sent.append({"url": url, "json": json, "headers": headers})
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]

    monkeypatch.setattr("agent.llm.requests.post", post)
    llm = OpenAICompatLLM(
        base_url=kw.pop("base_url", _endpoint()), model="m-8b", family="Llama", **kw
    )
    llm._sleep = lambda _: None  # type: ignore[method-assign]
    return llm, sent


def test_request_shape_is_deterministic_json_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, sent = _llm(monkeypatch, [_Resp(200)], api_key="sk-secret")
    assert llm.complete("SYS", "USER") == '{"action": "hold"}'
    req = sent[0]
    assert req["url"].endswith("/v1/chat/completions")
    body = req["json"]
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert body["temperature"] == 0.0 and body["seed"] == 7 and body["max_tokens"] == 220
    assert body["response_format"] == {"type": "json_object"}
    assert req["headers"] == {"Authorization": "Bearer sk-secret"}
    assert "sk-secret" not in repr(llm)


def test_a_base_url_ending_in_v1_is_not_doubled(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, sent = _llm(monkeypatch, [_Resp(200)], base_url="http://ollama:11434/v1/")
    llm.complete("s", "u")
    assert sent[0]["url"] == "http://ollama:11434/v1/chat/completions"
    assert sent[0]["headers"] == {}  # no key, no header


def test_a_busy_endpoint_is_an_outage_retried_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm, sent = _llm(monkeypatch, [_Resp(429, {}, {"Retry-After": "3"})] * 3)
    with pytest.raises(ModelUnavailable) as e:
        llm.complete("s", "u")
    assert len(sent) == 3 and e.value.retry_after == 3.0


@pytest.mark.parametrize(
    "failure",
    [requests.ReadTimeout("Read timed out"), _Resp(401, {}), _Resp(403, {})],
    ids=["timeout", "401", "403"],
)
def test_outages_that_a_retry_would_repeat_are_sent_once(
    monkeypatch: pytest.MonkeyPatch, failure: Any
) -> None:
    llm, sent = _llm(monkeypatch, [failure, _Resp(200)])
    with pytest.raises(ModelUnavailable):
        llm.complete("s", "u")
    assert len(sent) == 1


@pytest.mark.parametrize(
    "resp",
    [_Resp(400, {"error": "bad"}), _Resp(200, {"choices": []}), _Resp(200, ValueError("html"))],
    ids=["400", "no-choices", "not-json"],
)
def test_an_unusable_answer_is_an_executor_error_not_an_outage(
    monkeypatch: pytest.MonkeyPatch, resp: _Resp
) -> None:
    llm, _ = _llm(monkeypatch, [resp])
    with pytest.raises(ExecutorError) as e:
        llm.complete("s", "u")
    assert not isinstance(e.value, ModelUnavailable)


def test_family_is_required() -> None:
    with pytest.raises(ValueError, match="Rule 3"):
        OpenAICompatLLM(base_url=_endpoint(), model="m", family=" ")


def test_one_endpoint_shares_one_concurrency_cap() -> None:
    url = _endpoint()
    a = OpenAICompatLLM(base_url=url, model="a", family="x")
    b = OpenAICompatLLM(base_url=url, model="b", family="y")
    assert a._slots is b._slots and a._bucket is b._bucket


def test_token_bucket_waits_instead_of_collecting_429s() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(d: float) -> None:
        slept.append(d)
        now[0] += d

    bucket = TokenBucket(rate_per_min=60, burst=2, clock=lambda: now[0], sleep=sleep)
    bucket.acquire()
    bucket.acquire()  # the burst
    assert slept == []
    bucket.acquire()  # one per second after that
    assert slept == [pytest.approx(1.0)]
    TokenBucket(0).acquire()  # off


# --- make_llm ----------------------------------------------------------------------
def test_ollama_is_the_default_and_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_BACKEND", raising=False)
    monkeypatch.setenv("OLLAMA_MODEL", "llama3:8b-instruct-q4_K_M")
    monkeypatch.setenv("VERIFIER_MODEL", "qwen2.5-coder:7b")  # skips the /api/tags lookup
    executor = make_llm("executor")
    verifier = make_llm("verifier", executor_model=executor.name)
    assert model_backend() == "ollama"
    assert isinstance(executor, OllamaLLM) and executor.name == "llama3:8b-instruct-q4_K_M"
    assert isinstance(verifier, OllamaLLM) and verifier.name == "qwen2.5-coder:7b"
    assert family_of(executor) is None  # Ollama tags carry their family in the name


def test_openai_backend_builds_both_roles_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_BACKEND", "openai")
    for role, model, family in (
        ("EXECUTOR", "meta-llama/Llama-3.1-8B", "llama"),
        ("VERIFIER", "Qwen/Qwen2.5-7B", "qwen"),
    ):
        monkeypatch.setenv(f"{role}_BASE_URL", "http://vllm:8000")
        monkeypatch.setenv(f"{role}_OPENAI_MODEL", model)
        monkeypatch.setenv(f"{role}_FAMILY", family)
    executor = make_llm("executor")
    verifier = make_llm("verifier")
    assert isinstance(executor, OpenAICompatLLM) and executor.family == "llama"
    assert isinstance(verifier, OpenAICompatLLM) and verifier.name == "Qwen/Qwen2.5-7B"


def test_openai_backend_without_a_family_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_BACKEND", "openai")
    monkeypatch.setenv("EXECUTOR_BASE_URL", "http://vllm:8000")
    monkeypatch.setenv("EXECUTOR_OPENAI_MODEL", "meta-llama/Llama-3.1-8B")
    monkeypatch.delenv("EXECUTOR_FAMILY", raising=False)
    with pytest.raises(ValueError, match="EXECUTOR_FAMILY"):
        make_llm("executor")


def test_an_unknown_backend_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_BACKEND", "anthropic-direct")
    with pytest.raises(ValueError, match="MODEL_BACKEND"):
        make_llm("executor")


# --- Rule 3 on declared families -------------------------------------------------------
def test_two_sizes_of_one_family_are_not_distinct_models() -> None:
    """By name these differ; by declared family they are the same weights' family."""
    verifier_llm = OpenAICompatLLM(
        base_url=_endpoint(), model="meta-llama/Llama-3.1-70B-Instruct", family="Llama"
    )
    v = Verifier(
        verifier_llm, executor_model="meta-llama/Llama-3.1-8B-Instruct", executor_family="llama"
    )
    record = v.independence.as_dict()
    assert record["distinct_model"] is False
    assert record["executor_family"] == record["verifier_family"] == "llama"
    assert record["independent"] is True  # carried by the distinct framing alone
    with pytest.raises(VerifierError, match="Rule 3"):
        Verifier(
            verifier_llm,
            executor_model="meta-llama/Llama-3.1-8B-Instruct",
            executor_family="llama",
            distinct_framing=False,
        )


def test_declared_different_families_are_distinct() -> None:
    verifier_llm = OpenAICompatLLM(base_url=_endpoint(), model="Qwen/Qwen2.5-7B", family="qwen")
    v = Verifier(verifier_llm, executor_model="meta-llama/Llama-3.1-8B", executor_family="llama")
    assert v.independence.distinct_model is True


def test_ollama_tags_still_judge_family_by_name() -> None:
    v = Verifier(OllamaLLM(model="qwen2.5-coder:7b"), executor_model="llama3:8b-instruct-q4_K_M")
    assert v.independence.as_dict()["executor_family"] == "llama3"
    assert v.independence.distinct_model is True

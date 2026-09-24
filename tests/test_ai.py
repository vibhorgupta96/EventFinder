from __future__ import annotations

import json

import httpx
import pytest
from eventfinder.ai import (
    AIUnavailable,
    FallbackClassifier,
    GeminiClassifier,
    GroqClassifier,
    make_classifier,
)
from eventfinder.config import AIConfig, Settings
from eventfinder.domain import AIClassification, EventType


@pytest.mark.asyncio
async def test_fallback_uses_next_provider_after_failure(candidate):
    class Broken:
        async def classify(self, _candidate):
            raise AIUnavailable("rate limited")

    class Valid:
        async def classify(self, _candidate):
            return AIClassification(
                is_technical=True,
                compatible_eligibility=True,
                event_type=EventType.MEETUP,
                topics=["AI"],
                concise_summary="Technical AI discussion.",
                rationale="Technical agenda is explicit.",
            )

    result = await FallbackClassifier([Broken(), Valid()]).classify(candidate)
    assert result.is_technical is True
    assert result.event_type == EventType.MEETUP


@pytest.mark.asyncio
async def test_malformed_ai_json_is_rejected(candidate):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "not json"}]}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = GeminiClassifier("test-key", client, 5)
        with pytest.raises(AIUnavailable, match="invalid structured"):
            await classifier.classify(candidate)


@pytest.mark.asyncio
async def test_provider_and_model_are_recorded_in_classification(candidate, config, organizers):
    from eventfinder.policy import assess_candidate

    candidate.event_type = EventType.UNKNOWN

    class Classifier:
        async def classify(self, _candidate):
            return AIClassification(
                is_technical=True,
                compatible_eligibility=True,
                event_type=EventType.MEETUP,
                topics=["AI"],
                concise_summary="Engineering AI workshop.",
                rationale="The agenda is technical.",
                provider="fixture",
                model="fixture-model",
            )

    assessment = await assess_candidate(candidate, config, organizers, classifier=Classifier())
    assert assessment.ai_provenance == {
        "provider": "fixture",
        "model": "fixture-model",
        "rationale": "The agenda is technical.",
    }


def _groq_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(payload)}}]},
    )


@pytest.mark.asyncio
async def test_groq_classifier_success_parses_well_formed_completion(candidate):
    payload = {
        "is_technical": True,
        "compatible_eligibility": True,
        "event_type": "workshop",
        "topics": ["AI", "LLM"],
        "concise_summary": "Hands-on LLM systems workshop.",
        "rationale": "Explicit hands-on technical agenda.",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openai/v1/chat/completions"
        return _groq_response(payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = GroqClassifier("test-key", client, 5)
        result = await classifier.classify(candidate)

    assert result.is_technical is True
    assert result.compatible_eligibility is True
    assert result.event_type == EventType.WORKSHOP
    assert result.topics == ["AI", "LLM"]
    assert result.concise_summary == "Hands-on LLM systems workshop."
    assert result.rationale == "Explicit hands-on technical agenda."
    assert result.provider == "groq"
    assert result.model == "openai/gpt-oss-20b"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "payload"),
    [
        (
            "missing_required_field",
            {
                "is_technical": True,
                # compatible_eligibility is required and omitted here.
                "event_type": "meetup",
                "topics": [],
                "concise_summary": "ok",
                "rationale": "ok",
            },
        ),
        (
            "wrong_type_for_boolean_field",
            {
                "is_technical": [],
                "compatible_eligibility": True,
                "event_type": "meetup",
                "topics": [],
                "concise_summary": "ok",
                "rationale": "ok",
            },
        ),
        (
            "over_length_concise_summary",
            {
                "is_technical": True,
                "compatible_eligibility": True,
                "event_type": "meetup",
                "topics": [],
                "concise_summary": "x" * 401,
                "rationale": "ok",
            },
        ),
        (
            "over_length_rationale",
            {
                "is_technical": True,
                "compatible_eligibility": True,
                "event_type": "meetup",
                "topics": [],
                "concise_summary": "ok",
                "rationale": "x" * 401,
            },
        ),
    ],
)
async def test_groq_schema_violation_raises_ai_unavailable(candidate, label, payload):
    def handler(_request: httpx.Request) -> httpx.Response:
        return _groq_response(payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = GroqClassifier("test-key", client, 5)
        with pytest.raises(AIUnavailable, match="invalid structured"):
            await classifier.classify(candidate)


@pytest.mark.asyncio
async def test_groq_4xx_response_raises_ai_unavailable(candidate):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = GroqClassifier("test-key", client, 5)
        with pytest.raises(AIUnavailable, match="Groq returned 400"):
            await classifier.classify(candidate)


@pytest.mark.asyncio
async def test_groq_timeout_propagates_unwrapped_from_the_classifier(candidate):
    """GroqClassifier.classify does not wrap transport errors: only the response-parsing
    branch raises AIUnavailable. A timeout during the request itself surfaces as the raw
    httpx exception, matching the actual (not assumed) behavior of the code."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = GroqClassifier("test-key", client, 5)
        with pytest.raises(httpx.ReadTimeout):
            await classifier.classify(candidate)


@pytest.mark.asyncio
async def test_groq_timeout_is_reported_as_ai_unavailable_via_fallback_classifier(candidate):
    """FallbackClassifier is the layer that turns a transport-level timeout into the
    documented AIUnavailable signal, by catching httpx.HTTPError around each provider."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = GroqClassifier("test-key", client, 5)
        with pytest.raises(AIUnavailable, match="timed out"):
            await FallbackClassifier([classifier]).classify(candidate)


@pytest.mark.asyncio
async def test_make_classifier_falls_through_provider_order_to_groq():
    settings = Settings(gemini_api_key=None, groq_api_key="groq-secret")
    config = AIConfig(provider_order=["gemini", "groq"])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as client:
        classifier = make_classifier(settings, config, client)

    assert len(classifier.classifiers) == 1
    assert isinstance(classifier.classifiers[0], GroqClassifier)


@pytest.mark.asyncio
async def test_make_classifier_end_to_end_falls_through_to_groq_when_gemini_fails(candidate):
    settings = Settings(gemini_api_key="gemini-secret", groq_api_key="groq-secret")
    config = AIConfig(provider_order=["gemini", "groq"])
    groq_payload = {
        "is_technical": True,
        "compatible_eligibility": True,
        "event_type": "meetup",
        "topics": [],
        "concise_summary": "Fallback via Groq.",
        "rationale": "Gemini was unavailable.",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "generativelanguage.googleapis.com" in str(request.url):
            return httpx.Response(500, json={"error": "unavailable"})
        return _groq_response(groq_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        classifier = make_classifier(settings, config, client)
        result = await classifier.classify(candidate)

    assert result.provider == "groq"
    assert result.model == "openai/gpt-oss-20b"


@pytest.mark.asyncio
async def test_make_classifier_with_no_providers_configured_yields_empty_fallback():
    settings = Settings(gemini_api_key=None, groq_api_key=None)
    config = AIConfig(provider_order=["gemini", "groq"])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as client:
        classifier = make_classifier(settings, config, client)

    assert classifier.classifiers == []


@pytest.mark.asyncio
async def test_fallback_classifier_with_no_providers_raises_ai_unavailable(candidate):
    with pytest.raises(AIUnavailable, match="no AI provider is configured"):
        await FallbackClassifier([]).classify(candidate)

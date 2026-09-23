from __future__ import annotations

import httpx
import pytest
from eventfinder.ai import AIUnavailable, FallbackClassifier, GeminiClassifier
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

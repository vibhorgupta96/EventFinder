"""Schema-constrained ambiguity classification with Gemini then Groq fallback."""

from __future__ import annotations

import json
from typing import Protocol

import httpx
from pydantic import ValidationError

from eventfinder.config import AIConfig, Settings
from eventfinder.domain import AIClassification, EventCandidate


class AIUnavailable(RuntimeError):
    pass


class AIClassifier(Protocol):
    async def classify(self, candidate: EventCandidate) -> AIClassification:
        """Return only classification facts inferred from the supplied source record."""


def _prompt(candidate: EventCandidate) -> str:
    source_facts = {
        "title": candidate.title,
        "description": candidate.description,
        "organizer": candidate.organizer,
        "eligibility_text": candidate.eligibility_text,
        "price_text": candidate.price_text,
        "format": candidate.format.value,
        "event_type": candidate.event_type.value,
    }
    return (
        "Classify this public event candidate. Do not invent or infer dates, prices, speakers, venues, "
        "registration state, organizer facts, or eligibility facts. Return exactly JSON with keys "
        "Social-only mixers/networking gatherings and certification, credential or exam-preparation "
        "promotions are not technical events. Require substantive engineering content; incidental "
        "speaker credentials do not exclude a technical talk. Never infer free admission. "
        "is_technical (boolean), compatible_eligibility (boolean), event_type (talk|meetup|workshop|"
        "conference|hackathon|buildathon|competition|unknown), topics (array of short strings), "
        "concise_summary (max 400 chars), rationale (max 400 chars).\nSource record:\n"
        + json.dumps(source_facts, ensure_ascii=False)
    )


def _json_object(content: str) -> dict:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(cleaned)


class GeminiClassifier:
    def __init__(self, api_key: str, client: httpx.AsyncClient, timeout_seconds: int):
        self.api_key = api_key
        self.client = client
        self.timeout_seconds = timeout_seconds

    async def classify(self, candidate: EventCandidate) -> AIClassification:
        response = await self.client.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            params={"key": self.api_key},
            json={
                "contents": [{"parts": [{"text": _prompt(candidate)}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
            },
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise AIUnavailable(f"Gemini returned {response.status_code}")
        try:
            content = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            return AIClassification.model_validate(_json_object(content)).model_copy(
                update={"provider": "gemini", "model": "gemini-2.0-flash"}
            )
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as error:
            raise AIUnavailable("Gemini returned an invalid structured result") from error


class GroqClassifier:
    def __init__(self, api_key: str, client: httpx.AsyncClient, timeout_seconds: int):
        self.api_key = api_key
        self.client = client
        self.timeout_seconds = timeout_seconds

    async def classify(self, candidate: EventCandidate) -> AIClassification:
        response = await self.client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": "openai/gpt-oss-20b",
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": _prompt(candidate)}],
            },
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise AIUnavailable(f"Groq returned {response.status_code}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
            return AIClassification.model_validate(_json_object(content)).model_copy(
                update={"provider": "groq", "model": "openai/gpt-oss-20b"}
            )
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as error:
            raise AIUnavailable("Groq returned an invalid structured result") from error


class FallbackClassifier:
    """Calls configured providers in order. No API key means no provider attempt."""

    def __init__(self, classifiers: list[AIClassifier]):
        self.classifiers = classifiers

    async def classify(self, candidate: EventCandidate) -> AIClassification:
        errors: list[str] = []
        for classifier in self.classifiers:
            try:
                return await classifier.classify(candidate)
            except (AIUnavailable, httpx.HTTPError) as error:
                errors.append(str(error))
        raise AIUnavailable("; ".join(errors) or "no AI provider is configured")


def make_classifier(settings: Settings, config: AIConfig, client: httpx.AsyncClient) -> FallbackClassifier:
    classifiers: list[AIClassifier] = []
    for provider in config.provider_order:
        if provider == "gemini" and settings.gemini_api_key:
            classifiers.append(GeminiClassifier(settings.gemini_api_key, client, config.timeout_seconds))
        if provider == "groq" and settings.groq_api_key:
            classifiers.append(GroqClassifier(settings.groq_api_key, client, config.timeout_seconds))
    return FallbackClassifier(classifiers)

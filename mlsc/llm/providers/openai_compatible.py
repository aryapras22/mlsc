"""One provider implementation for any OpenAI-compatible endpoint.

Covers Ollama, vLLM, LM Studio, and OpenAI itself: all serve the same
chat-completions API shape, differing only in ``base_url`` and ``api_key``
(requirement 1 — configuration selects the target, not code).
"""

from __future__ import annotations

import json
from typing import TypeVar

from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from mlsc.llm.base import Completion, ProviderUnreachable, SchemaViolation

T = TypeVar("T", bound=BaseModel)


class OpenAiCompatibleProvider:
    def __init__(self, client: AsyncOpenAI, *, model: str) -> None:
        self._client = client
        self._model = model

    async def complete(self, *, prompt: str, schema: type[T], prompt_version: str) -> Completion:
        """One retry on a schema violation; on the second failure, raise.

        Requirement 2: no partial result is ever persisted for a completion
        that never validates.
        """
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                raw = await self._request(prompt)
            except (APIConnectionError, APIStatusError) as error:
                raise ProviderUnreachable(str(error)) from error
            try:
                parsed = schema.model_validate_json(raw)
            except (ValidationError, json.JSONDecodeError) as error:
                last_error = error
                continue
            return Completion(
                value=parsed, provider="openai_compatible", model=self._model,
                prompt_version=prompt_version,
            )
        raise SchemaViolation(str(last_error))

    async def _request(self, prompt: str) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or "{}"

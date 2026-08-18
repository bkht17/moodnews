"""Thin wrapper around an OpenAI-compatible chat-completions endpoint.

The app never names a provider. It assumes only the OpenAI
`/chat/completions` contract, and takes the base URL, model and credentials
from the environment, so GLM (z.ai), OpenAI, Together, a local vLLM or an
in-house gateway are all a `.env` change rather than a code change:

    LLM_BASE_URL=https://api.z.ai/api/paas/v4
    LLM_MODEL=glm-4.6

Both callers - the mood rewriter and the fact-checking auditor - want a JSON
object back, so that is the only method exposed. Structured output is
requested through `response_format={"type": "json_object"}` where the endpoint
supports it, with a text-parsing fallback for endpoints that reject the
parameter, because not every OpenAI-compatible server implements it.
"""

import json
import logging
import re
from functools import lru_cache
from typing import Any

from openai import APIError, APIStatusError, BadRequestError, OpenAI

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Any failure to obtain a usable answer from the model."""


class LLMNotConfigured(LLMError):
    """No API key is set, so rewriting is unavailable.

    Kept distinct from LLMError so the API layer can answer 503 with a clear
    "configure LLM_API_KEY" message instead of a generic upstream failure.
    """


# --- JSON recovery ----------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_object(raw: str) -> dict[str, Any]:
    """Parse a JSON object out of a model reply.

    Models that honour json_object mode return clean JSON and the first
    `json.loads` succeeds. The rest of this function exists for the ones that
    wrap it in a code fence or a sentence of preamble.
    """
    text = (raw or "").strip()
    if not text:
        raise LLMError("Model returned an empty response")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = _FENCE.search(text)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Last resort: the outermost balanced {...} in the reply.
    start = text.find("{")
    if start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        break

    raise LLMError(f"Model reply was not valid JSON: {text[:200]}")


# --- Client -----------------------------------------------------------------


class LLMClient:
    """Chat-completions client that always returns a parsed JSON object."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 90.0,
    ) -> None:
        if not api_key:
            raise LLMNotConfigured(
                "LLM_API_KEY is not set. Copy .env.example to .env and add a "
                "key to enable mood rewriting."
            )
        self.model = model
        self.base_url = base_url
        # The SDK retries connection errors and 5xx on its own; one extra
        # attempt is enough given callers have their own retry policy.
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=1,
        )
        # Set to False the first time the endpoint rejects json_object mode,
        # so we stop paying for a doomed round trip on every later call.
        self._supports_json_mode = True

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """One chat completion, returned as a parsed JSON object."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self._supports_json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = self._client.chat.completions.create(**kwargs)
        except BadRequestError as exc:
            # Most likely the endpoint does not implement json_object mode.
            if self._supports_json_mode:
                logger.warning(
                    "Endpoint rejected json_object mode (%s); falling back to "
                    "plain completions with JSON parsing.",
                    exc,
                )
                self._supports_json_mode = False
                kwargs.pop("response_format", None)
                try:
                    response = self._client.chat.completions.create(**kwargs)
                except APIError as retry_exc:
                    raise LLMError(f"LLM request failed: {retry_exc}") from retry_exc
            else:
                raise LLMError(f"LLM rejected the request: {exc}") from exc
        except APIStatusError as exc:
            raise LLMError(
                f"LLM returned HTTP {exc.status_code}: {exc.message}"
            ) from exc
        except APIError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        if not response.choices:
            raise LLMError("LLM returned no choices")

        content = response.choices[0].message.content or ""
        payload = parse_json_object(content)
        if not isinstance(payload, dict):
            raise LLMError("Model returned JSON that is not an object")
        return payload


@lru_cache
def get_llm_client() -> LLMClient:
    """Process-wide client. Raises LLMNotConfigured when no key is set."""
    settings = get_settings()
    return LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
    )

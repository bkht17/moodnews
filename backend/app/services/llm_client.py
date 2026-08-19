"""LLM access, behind one small interface with two backends.

Both callers - the mood rewriter and the fact-checking auditor - want the same
thing: a JSON object matching a known shape. That is the entire interface
(`complete_json`), which keeps the pipeline ignorant of who is answering.

Two backends implement it:

  * `OpenAICompatibleClient` - anything speaking the OpenAI
    `/chat/completions` contract: GLM (z.ai), OpenAI, Gemini's compatibility
    endpoint, Groq, a local Ollama or vLLM. JSON is requested through
    `response_format={"type": "json_object"}` where supported, with a
    text-parsing fallback for endpoints that reject or ignore the parameter.

  * `AnthropicClient` - Claude through the official Anthropic SDK. Worth its
    own backend rather than going through Anthropic's OpenAI compatibility
    shim, which ignores `response_format` entirely: the native SDK enforces the
    response schema server-side, so a malformed rewrite payload cannot reach
    the fact-checker in the first place. It also surfaces policy refusals as a
    distinct outcome, which a news app rewriting reports of violence will meet
    sooner or later.

Which one runs is `LLM_PROVIDER`, defaulting to "auto": a `claude-*` model name
or Anthropic's host selects the native client, anything else the OpenAI one. So
switching provider stays a `.env` change:

    LLM_BASE_URL=https://api.z.ai/api/paas/v4   LLM_MODEL=glm-4.6
    LLM_MODEL=claude-opus-5                     (base URL unused)
"""

import json
import logging
import re
from functools import lru_cache
from typing import Any

from openai import APIError, APIStatusError, BadRequestError, OpenAI
from pydantic import BaseModel

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Any failure to obtain a usable answer from the model."""


class LLMNotConfigured(LLMError):
    """No API key is set, so rewriting is unavailable.

    Kept distinct from LLMError so the API layer can answer 503 with a clear
    "configure LLM_API_KEY" message instead of a generic upstream failure.
    """


class LLMRefused(LLMError):
    """The model declined to answer on policy grounds.

    Distinct because it is not a fault: real news covers violence and death,
    and a refusal on one article in one mood says nothing about the service
    being healthy. The API reports it as a per-rewrite error beside a perfectly
    readable original, rather than as a failure of the request.
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


class OpenAICompatibleClient:
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
        schema: type[BaseModel] | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """One chat completion, returned as a parsed JSON object.

        `schema` and `effort` are honoured by the Anthropic backend; here they
        are accepted and ignored, so callers can state their intent once and
        stay backend-agnostic.
        """
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


# --- Anthropic backend ------------------------------------------------------


class AnthropicClient:
    """Claude through the official Anthropic SDK.

    Three things this gets that the OpenAI compatibility shim does not:

    * **Schema-enforced JSON.** `messages.parse(output_format=...)` validates
      the reply against the caller's model server-side. The shim ignores
      `response_format` outright, which would leave a fact-check pipeline
      parsing whatever prose came back.
    * **Refusal as a distinct outcome.** `stop_reason == "refusal"` is reported
      as `LLMRefused` rather than as a parse failure. Rewriting real news means
      handling articles about killings and disasters, so this will happen.
    * **Server-side fallbacks.** A refusal is retried on another Claude model
      inside the same call, so one declined article does not become a dead
      panel in the UI.

    Sampling note: current Claude models removed `temperature` (Claude 5
    rejects it outright), so the parameter is accepted from callers and not
    forwarded. Determinism for the fact-checking pass does not depend on it -
    the binding check is the regex layer, which is deterministic by
    construction; this call is the second opinion on top of it, and runs at
    high effort instead.
    """

    # Claude models reason before answering, and that reasoning is drawn from
    # the same budget as the answer. A ceiling sized for the visible reply
    # alone would truncate mid-thought.
    MIN_MAX_TOKENS = 16000

    def __init__(self, api_key: str, model: str, timeout: float = 90.0) -> None:
        if not api_key:
            raise LLMNotConfigured(
                "LLM_API_KEY is not set. Copy .env.example to .env and add an "
                "Anthropic API key to enable mood rewriting."
            )
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LLMError(
                "The 'anthropic' package is required for LLM_PROVIDER=anthropic. "
                "Install it with: pip install -r requirements.txt"
            ) from exc

        self._anthropic = anthropic
        self.model = model
        self.base_url = "https://api.anthropic.com"
        self._client = anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout,
            max_retries=1,
        )
        # Downgraded once and remembered if the account or SDK does not accept
        # the beta request shape, so a mismatch costs one round trip, not one
        # per call.
        self._use_fallbacks = True

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int = 4096,
        schema: type[BaseModel] | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """One message, returned as a parsed JSON object.

        With a `schema` the reply is schema-validated by the API; without one
        the model is asked for JSON in the prompt and the text is parsed here,
        so the contract matches the OpenAI backend either way.
        """
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max(max_tokens, self.MIN_MAX_TOKENS),
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if effort:
            request["output_config"] = {"effort": effort}

        try:
            response = self._send(request, schema)
        except self._anthropic.AuthenticationError as exc:
            raise LLMError(
                f"Anthropic rejected the API key: {exc}. Check LLM_API_KEY and "
                "that the key's account has credit."
            ) from exc
        except self._anthropic.RateLimitError as exc:
            raise LLMError(f"Anthropic rate limit reached: {exc}") from exc
        except self._anthropic.APIStatusError as exc:
            raise LLMError(
                f"Anthropic returned HTTP {exc.status_code}: {exc.message}"
            ) from exc
        except self._anthropic.APIError as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc

        # A refusal that survived the fallback chain: the whole chain declined.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise LLMRefused(
                f"The model declined to process this article ({category}). "
                "This can happen with distressing subject matter; the original "
                "article is unaffected."
            )

        parsed = getattr(response, "parsed_output", None)
        if parsed is not None:
            return parsed.model_dump()

        # No schema was given (or the SDK returned text only): recover JSON
        # from the text blocks exactly as the OpenAI backend does.
        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        payload = parse_json_object(text)
        if not isinstance(payload, dict):
            raise LLMError("Model returned JSON that is not an object")
        return payload

    def _send(self, request: dict[str, Any], schema: type[BaseModel] | None):
        """Issue the request, degrading gracefully if a shape is unsupported.

        Preference order: schema-enforced output with refusal fallbacks, then
        without fallbacks, then plain text. Each downgrade is remembered.
        """
        if schema is not None:
            if self._use_fallbacks:
                try:
                    return self._client.beta.messages.parse(
                        output_format=schema,
                        # Rescue a policy refusal on another Claude model
                        # in-call rather than failing the rewrite outright.
                        betas=["server-side-fallback-2026-07-01"],
                        fallbacks="default",
                        **request,
                    )
                except (self._anthropic.BadRequestError, TypeError) as exc:
                    logger.warning(
                        "Anthropic rejected the refusal-fallback request shape "
                        "(%s); continuing without it.",
                        exc,
                    )
                    self._use_fallbacks = False
            return self._client.messages.parse(output_format=schema, **request)

        return self._client.messages.create(**request)


@lru_cache
def get_llm_client() -> "OpenAICompatibleClient | AnthropicClient":
    """Process-wide client for the configured provider.

    Raises LLMNotConfigured when no key is set.
    """
    settings = get_settings()
    if settings.resolved_llm_provider == "anthropic":
        return AnthropicClient(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
        )
    return OpenAICompatibleClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
    )

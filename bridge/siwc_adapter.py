"""Local SIWC adapter; strict completion, actual model/usage and serialized refresh."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from openai import OpenAI, APIStatusError
from bridge.adapters import AdapterError, STRUCTURED_FIELDS
from bridge.credential_store import TokenStore

MODEL_CALL_TIMEOUT = 120
MAX_OUTPUT_CHARS = 50000
FORBIDDEN_FIELDS = {"background", "conversation", "previous_response_id", "max_output_tokens",
    "max_tool_calls", "metadata", "moderation", "multi_agent", "prompt", "prompt_cache_retention",
    "safety_identifier", "temperature", "top_logprobs", "top_p", "truncation", "user"}


def _default_token_file():
    return Path(os.environ.get("SIWC_TOKEN_FILE", Path.home() / ".muse-gpt-bridge" / "siwc_tokens.json"))


def validate_reply(value):
    if not isinstance(value, dict) or set(value) != set(STRUCTURED_FIELDS):
        raise AdapterError("Structured response must contain exactly five fields")
    if any(not isinstance(value[k], str) for k in ("reply", "proposed_next_step", "status")):
        raise AdapterError("Structured text fields must be strings")
    if type(value["needs_user_decision"]) is not bool:
        raise AdapterError("needs_user_decision must be boolean")
    if not isinstance(value["questions_for_muse"], list) or any(not isinstance(q, str) for q in value["questions_for_muse"]):
        raise AdapterError("questions_for_muse must contain only strings")
    if value["status"] not in ("continue", "waiting_user", "done", "failed"):
        raise AdapterError("Invalid structured response status")
    return value


class SIWCAdapter:
    name = "siwc"
    conversation_prefix = "siwc"
    base_url = "https://api.openai.com/v1"
    token_url = "https://auth.openai.com/api/accounts/oauth/token"

    def __init__(self, model=None, token_file=None, token_store=None):
        if os.environ.get("SIWC_BASE_URL", self.base_url) != self.base_url or os.environ.get("SIWC_TOKEN_URL", self.token_url) != self.token_url:
            raise AdapterError("SIWC credentials may only be sent to official OpenAI endpoints")
        self.tokens = token_store or TokenStore(token_file or _default_token_file())
        self._refresh_lock = threading.RLock()
        self._last_usage, self._last_actual_model = {}, None
        self._last_is_retry = False
        self.last_success_at = None
        try:
            self._adopt(self.tokens.read())
        except (OSError, ValueError) as exc:
            raise AdapterError("Cannot read protected local SIWC credentials") from None
        self.model = model or os.environ.get("SIWC_MODEL")
        if not self.model:
            raise AdapterError("SIWC_MODEL must be explicitly selected from the account's visible models")

    def _adopt(self, record):
        if not record.get("access_token"):
            raise AdapterError("未配置 SIWC token, 拒绝调用 (不伪造回复)")
        if not record.get("id_token_signature_verified"):
            raise AdapterError("SIWC identity has not been signature-verified")
        if "chatgpt.tokens.use.direct" not in record.get("scopes", []):
            raise AdapterError("ChatGPT plan permission is missing")
        self._record = record
        self._access_token = record["access_token"]
        self._expires_at = record.get("expires_at")

    def redact(self, value):
        text = str(value)
        for key in ("access_token", "refresh_token", "id_token"):
            if self._record.get(key):
                text = text.replace(self._record[key], "[REDACTED]")
        text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED_JWT]", text)
        return text[:1000]

    def refresh_access_token(self, force=False):
        try:
            with self._refresh_lock, self.tokens.locked():
                latest = self.tokens.read()
                old_access = self._access_token
                self._adopt(latest)
                if old_access != self._access_token:
                    return True
                if not force and latest.get("expires_at", 0) > time.time() + 90:
                    return True
                if not latest.get("refresh_token") or not latest.get("client_id"):
                    raise AdapterError("Renewable SIWC registration is incomplete")
                request = urllib.request.Request(self.token_url, method="POST",
                    data=urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": latest["client_id"],
                        "refresh_token": latest["refresh_token"], "resource": self.base_url}).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    fresh = json.loads(response.read())
                if not fresh.get("access_token") or not fresh.get("refresh_token"):
                    raise AdapterError("Refresh did not return both replacement credentials")
                updated = {**latest, **fresh, "expires_at": int(time.time()) + int(fresh.get("expires_in", 3600))}
                if fresh.get("scope"):
                    updated["scopes"] = fresh["scope"].split()
                self.tokens.write(updated)
                self._adopt(updated)
                return True
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError("SIWC refresh failed: " + self.redact(type(exc).__name__)) from None

    def create_conversation(self, discussion_id):
        return "siwc-" + discussion_id

    def _build_input(self, discussion, messages, new_content, user_answer=None):
        items = []
        for message in messages:
            content = message.get("structured_json") if message.get("role") == "gpt" else message.get("content")
            items.append({"role": "assistant" if message.get("role") == "gpt" else "user", "content": content or message.get("content", "")})
        if user_answer:
            new_content += "\n用户回答: " + str(user_answer)
        items.append({"role": "user", "content": new_content})
        return items

    def _structured_schema(self):
        return {"type": "object", "properties": {
            "reply": {"type": "string"}, "questions_for_muse": {"type": "array", "items": {"type": "string"}},
            "needs_user_decision": {"type": "boolean"}, "proposed_next_step": {"type": "string"},
            "status": {"type": "string", "enum": ["continue", "waiting_user", "done", "failed"]}},
            "required": STRUCTURED_FIELDS, "additionalProperties": False}

    def _post_responses(self, payload):
        if FORBIDDEN_FIELDS.intersection(payload):
            raise AdapterError("SIWC payload contains unsupported fields")
        start, parts, completed = time.monotonic(), [], None
        try:
            with OpenAI(api_key=self._access_token, base_url=self.base_url, max_retries=0, timeout=MODEL_CALL_TIMEOUT) as client:
                with client.responses.create(**payload) as stream:
                    for event in stream:
                        if time.monotonic() - start > MODEL_CALL_TIMEOUT:
                            raise AdapterError("SIWC round exceeded the local time limit")
                        kind = getattr(event, "type", "")
                        if kind == "response.output_text.delta":
                            parts.append(getattr(event, "delta", ""))
                            if sum(map(len, parts)) > MAX_OUTPUT_CHARS:
                                raise AdapterError("SIWC output exceeded the local size limit")
                        elif kind in ("response.failed", "response.incomplete", "error"):
                            error = getattr(getattr(event, "response", None), "error", None)
                            raise AdapterError("SIWC stream failed: " + self.redact(error or kind))
                        elif kind == "response.completed":
                            completed = event.response
        except APIStatusError as exc:
            raise _classify_http_error(exc.status_code, self.redact(str(exc))) from None
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError("SIWC network or stream error: " + self.redact(type(exc).__name__)) from None
        if completed is None:
            raise AdapterError("SIWC stream ended without response.completed")
        self._last_actual_model = getattr(completed, "model", None)
        if not self._last_actual_model:
            raise AdapterError("Completed SIWC response omitted its actual model")
        usage = getattr(completed, "usage", None)
        return "".join(parts), {"input_tokens": getattr(usage, "input_tokens", None), "output_tokens": getattr(usage, "output_tokens", None)}

    def reply(self, *, discussion, messages, new_content, user_answer=None, round_no):
        self._last_usage, self._last_actual_model, self._last_is_retry = {}, None, False
        self._adopt(self.tokens.read())
        if self._expires_at and self._expires_at <= time.time() + 90:
            self.refresh_access_token()
        payload = {"model": self.model, "instructions": (
            "你是与 Muse 讨论的外部顾问，问题交给 Muse 汇总。回复简洁，只返回约定 JSON；"
            "不要声称已检索实时价格。用户需求: " + discussion.get("original_request", "") +
            " 背景: " + discussion.get("user_context", "") + " 目标: " + discussion.get("project_goal", "")),
            "input": self._build_input(discussion, messages, new_content, user_answer),
            "stream": True, "store": False,
            "text": {"format": {"type": "json_schema", "name": "muse_reply", "schema": self._structured_schema(), "strict": True}}}
        try:
            text, usage = self._post_responses(payload)
        except AdapterError as exc:
            if not str(exc).startswith("SIWC-401"):
                raise
            self.refresh_access_token(force=True)
            self._last_is_retry = True
            text, usage = self._post_responses(payload)
        try:
            parsed = validate_reply(json.loads(text))
        except (ValueError, TypeError):
            raise AdapterError("SIWC reply was not valid structured JSON") from None
        self._last_usage = usage
        self.last_success_at = int(time.time())
        return parsed

    def cost(self):
        return {"model": self._last_actual_model, "requested_model": self.model,
                "prompt_tokens": self._last_usage.get("input_tokens"),
                "completion_tokens": self._last_usage.get("output_tokens"),
                "cost_cny": 0.0, "funding": "chatgpt-plus-plan-usage", "is_retry": self._last_is_retry}


def _classify_http_error(code, body):
    # No automatic retries for quota, eligibility or server errors in this stage.
    return AdapterError(f"SIWC-{code}: {body[:800]}")

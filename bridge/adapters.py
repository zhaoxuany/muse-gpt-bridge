"""GPT 调用层: 可插拔适配器.

- StubAdapter (默认): 不调任何网络, 返回明确标注的占位回复.
  绝不能把它的回复说成 GPT 的真实回复. cost 记 0, funding=stub.
- OpenAIAdapter (骨架): 读环境变量 OPENAI_API_KEY, 不存在时拒绝调用、
  不伪造回复. 只有在用户确认付款/订阅并批准后, 才由它真正调
  OpenAI Responses API + Conversations API. 本文件里的调用代码
  在本地测试中从未被执行过 (无 key).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

MODEL_CALL_TIMEOUT = 120  # 秒, 复用讨论层 MODEL_CALL_TIMEOUT
MAX_OUTPUT_TOKENS = 2000  # 单次调用最大输出, 仅 API 路线可用

STRUCTURED_FIELDS = ["reply", "questions_for_muse", "needs_user_decision",
                      "proposed_next_step", "status"]


class AdapterError(RuntimeError):
    pass


class StubAdapter:
    """占位适配器: 回显式回复, 结构化五字段齐全, 费用为 0."""

    name = "stub"
    conversation_prefix = "stub"

    def create_conversation(self, discussion_id: str) -> str:
        return f"stub-{discussion_id}"

    def reply(self, *, discussion: dict, messages: list, new_content: str,
              user_answer=None, round_no: int) -> dict:
        snippet = (new_content or "")[:200]
        if round_no <= 1:
            return {
                "reply": ("【STUB 占位回复, 不是 GPT 的真实回复】收到你的第一条消息: "
                          f"「{snippet}」. 先说思路: 耳机按用途和价位分三档比较, "
                          "宿舍用品按必需/可缓两类列清单, 再给具体型号."),
                "questions_for_muse": ["预算大概多少?", "宿舍桌子有多大?"],
                "needs_user_decision": True,
                "proposed_next_step": "等 Muse 补充预算和宿舍尺寸后再给具体型号",
                "status": "waiting_user",
            }
        extra = ""
        if user_answer:
            extra = f"已收到用户回答: 「{str(user_answer)[:200]}」, "
        return {
            "reply": ("【STUB 占位回复, 不是 GPT 的真实回复】"
                      f"{extra}基于你补充的内容「{snippet}」, 更新建议: "
                      "耳机三档对比不变, 宿舍用品清单按你的尺寸做了调整, "
                      "下一轮可给具体型号和价格来源."),
            "questions_for_muse": [],
            "needs_user_decision": False,
            "proposed_next_step": "给出三款耳机对比表和宿舍用品清单",
            "status": "continue",
        }

    def cost(self) -> dict:
        return {"model": "stub", "prompt_tokens": 0, "completion_tokens": 0,
                "cost_cny": 0.0, "funding": "stub"}


class OpenAIAdapter:
    """真实 OpenAI Responses API 适配器 —— 骨架, 未接通.

    要求 OPENAI_API_KEY 在环境变量里 (只存在服务端凭证, 不进源码/
    日志/聊天记录). 无 key 时拒绝调用. 模型默认 gpt-6.1-terra
    (gpt-5 系列 2026-12-11 下线, 新开发不要用 gpt-5/mini/nano).
    注意: 走 Sign in with ChatGPT 计划用量时, conversation / max_output_tokens
    等字段不可用, 本适配器只覆盖 API key 路线.
    """

    name = "openai"
    conversation_prefix = "conv"
    base_url = "https://api.openai.com/v1"

    def __init__(self, model: str = "gpt-6.1-terra"):
        self.api_key = os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise AdapterError("OPENAI_API_KEY 未设置, 拒绝调用 (不伪造回复)")
        self.model = model

    def _request(self, method: str, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            method=method,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=MODEL_CALL_TIMEOUT) as resp:
            return json.loads(resp.read().decode())

    def create_conversation(self, discussion_id: str) -> str:
        data = self._request("POST", "/conversations",
                             {"metadata": {"discussion_id": discussion_id}})
        return data["id"]

    def reply(self, *, discussion: dict, messages: list, new_content: str,
              user_answer=None, round_no: int) -> dict:
        schema = {
            "type": "object",
            "properties": {
                "reply": {"type": "string"},
                "questions_for_muse": {"type": "array", "items": {"type": "string"}},
                "needs_user_decision": {"type": "boolean"},
                "proposed_next_step": {"type": "string"},
                "status": {"type": "string",
                           "enum": ["continue", "waiting_user", "done", "failed"]},
            },
            "required": STRUCTURED_FIELDS,
            "additionalProperties": False,
        }
        instructions = (
            "你是外部技术顾问 GPT, 只与 Muse 对话, 绝不直接向用户提问. "
            f"用户原始需求: {discussion.get('original_request', '')} "
            f"个人背景: {discussion.get('user_context', '')} "
            f"项目目标: {discussion.get('project_goal', '')}"
        )
        payload = {
            "model": self.model,
            "conversation": discussion.get("openai_conversation_id"),
            "instructions": instructions,
            "input": new_content,
            "text": {"format": {"type": "json_schema", "name": "gpt_reply",
                                "schema": schema, "strict": True}},
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }
        t0 = time.time()
        data = self._request("POST", "/responses", payload)
        self._last_usage = data.get("usage", {})
        self._last_duration_ms = int((time.time() - t0) * 1000)
        return json.loads(data["output_text"])

    def cost(self) -> dict:
        usage = getattr(self, "_last_usage", {}) or {}
        # 价格未登记进 prices.json 前无法算费: fail-closed, 报 0 并标记未计价.
        return {"model": self.model,
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "cost_cny": 0.0, "funding": "openai-api-unpriced"}


def get_adapter():
    """默认 STUB. 只有显式设 MUSE_GPT_ADAPTER=openai 且有 key 时才用真适配器.
    MUSE_GPT_ADAPTER=siwc 时用 SIWCAdapter (见 bridge/siwc_adapter.py, 无 token 即拒绝)."""
    mode = os.environ.get("MUSE_GPT_ADAPTER", "stub")
    if mode == "openai":
        raise AdapterError("Paid API mode is outside this local acceptance scope")
    if mode == "siwc":
        from bridge.siwc_adapter import SIWCAdapter
        return SIWCAdapter()
    if mode == "stub":
        return StubAdapter()
    raise AdapterError("Unknown adapter mode; refusing to silently use STUB")

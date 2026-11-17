"""SIWC 真实调用测试 v2 (Windows 本地用, 只跑在用户自己电脑上).

v2.1 修订 (2026-11-17, Work GPT 复核发现的 5 个缺陷已修, 见 TEST_EVIDENCE_WINDOWS_FIX.md):
- F1: 补 import os (旧版启动即 NameError).
- F2: history 原地 append, 不再重新绑定; 第 1 轮给预算 500/桌子 60cm, 第 2 轮不再重述数字,
  只有 GPT 自己复述出来才算上下文延续.
- F3: 模型取 response.completed 的实际返回值, 用量缺失记 null 不补 0.
- F4: siwc_auth.py 用 PyJWT + JWKS 校验 ID token 签名, 不通过拒绝保存.
- F5: 每轮后立刻写结果文件, 中途崩了也有阶段记录.

v2 相对 v1 的变化:
- 授权脚本已按官方文档改对 (loopback 回调自动收授权码, 不再手动粘贴;
  换 token / 刷新改为 form-encoded, 端点改对; token 路径与桥统一).
- 对话扩展到 3 轮: 第 3 轮让 GPT 针对第 2 轮的回答追问一个需要用户决策的
  问题, 验证 “GPT 提问 -> Muse 汇总后问用户” 这条链路 (本脚本只验证到
  GPT 确实提出了需决策的问题并标了 needs_user_decision=true;
  Muse 汇总提问、用户回答、再传回 GPT, 要等桥接通后走桥的接口验证).
- 新增断网恢复演示 (--offline-demo): 第 2 轮后手动断网, 调一次应失败并被
  捕获, 恢复网络后用同一份历史续聊第 3 轮, 验证断线后能接着聊、不用重起炉灶.
- 用量记录: 结果 JSON 里每轮都有 model、input/output tokens、耗时、
  funding=chatgpt-plus-plan-usage; 费用 0 美元 (走 Plus 计划用量).

前置: 先跑 python siwc_auth.py 完成授权, token 文件已在
%USERPROFILE%\\.muse-gpt-bridge\\siwc_tokens.json.

本脚本全部是真实调用, 没有任何 STUB/模拟. SIWC preview 限制 (严格遵守):
- 每次调用设 store=False、stream=True.
- 不传 conversation、max_output_tokens、temperature、top_p 等被禁字段;
  输出长度靠 instructions 里“回复控制在 2000 tokens 以内”约束.
- 历史由本脚本自己维护 (history 列表), 不依赖 OpenAI 服务端会话.

结果保存在同目录 siwc_test_result.json (只含回复、用量、耗时、模型,
绝不含任何 token; 发回给 Muse 时直接发这个文件).

用法 (Windows PowerShell, 在 windows-siwc-test-v2 目录下):
    pip install -r requirements.txt
    python test_siwc.py                    # 跑第 1、2、3 轮
    python test_siwc.py --refresh          # 再加刷新演示
    python test_siwc.py --bad-model        # 再加失败恢复演示 (错模型名)
    python test_siwc.py --offline-demo     # 再加断网恢复演示 (要手动断网/恢复)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_FILE = (Path(os.environ["SIWC_TOKEN_FILE"]) if os.environ.get("SIWC_TOKEN_FILE")
              else Path.home() / ".muse-gpt-bridge" / "siwc_tokens.json")
RESULT_FILE = Path(__file__).resolve().parent / "siwc_test_result.json"

SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "questions_for_muse": {"type": "array", "items": {"type": "string"}},
        "needs_user_decision": {"type": "boolean"},
        "proposed_next_step": {"type": "string"},
        "status": {"type": "string",
                   "enum": ["continue", "waiting_user", "done", "failed"]},
    },
    "required": ["reply", "questions_for_muse", "needs_user_decision",
                 "proposed_next_step", "status"],
    "additionalProperties": False,
}

INSTRUCTIONS = (
    "你是外部技术顾问 GPT, 只与 Muse 对话, 绝不直接向用户提问. "
    "用户原始需求: 帮我研究购买耳机和宿舍用品. "
    "个人背景: 大一新生, 住学校宿舍, 预算有限. "
    "项目目标: 给出耳机推荐和宿舍用品清单. "
    "请只用结构化 JSON 回复, 不要输出 JSON 以外的文字. "
    "回复控制在 2000 tokens 以内."
)


def load_tokens() -> dict:
    if not TOKEN_FILE.exists():
        print(f"找不到 token 文件: {TOKEN_FILE}", file=sys.stderr)
        print("请先跑 python siwc_auth.py 完成授权.", file=sys.stderr)
        raise SystemExit(1)
    with open(TOKEN_FILE, encoding="utf-8") as f:
        data = json.load(f)
    if not data.get("access_token"):
        print("token 文件里没有 access_token, 请重新跑 siwc_auth.py.", file=sys.stderr)
        raise SystemExit(1)
    return data


def refresh_tokens(tokens: dict) -> dict:
    """刷新演示: 用 refresh_token 换新 token, 写回文件 (刷新后换新, 重新起算 30 天).
    刷新请求必须是 form-encoded (官方文档 "Refreshing tokens" 一节,
    2026-11-17 直接读过原文), 不能发 JSON; 带 resource, 省略 scope.
    注意: 刷新必须串行做, 本脚本是单进程跑, 不存在并发刷新问题."""
    print("\n--- 刷新演示: 正在用 refresh_token 换新 token ---")
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": tokens.get("client_id"),
        "resource": "https://api.openai.com/v1",
    }
    req = urllib.request.Request(
        "https://auth.openai.com/api/accounts/oauth/token",
        data=urllib.parse.urlencode(payload).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if "access_token" not in data:
        raise RuntimeError(f"刷新失败, 返回里没有 access_token: {str(data)[:200]}")
    tokens["access_token"] = data["access_token"]
    tokens["refresh_token"] = data.get("refresh_token", tokens["refresh_token"])
    tokens["expires_at"] = int(time.time()) + int(data.get("expires_in", 3600))
    # 原子写回 (tmp + os.replace), 避免中途崩了写坏 token 文件.
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens), encoding="utf-8")
    os.replace(tmp, TOKEN_FILE)
    print("刷新成功: 新 access_token 已生效 (约 1 小时), "
          "新 refresh_token 已保存 (重新起算 30 天).")
    print("注意: 旧 refresh_token 已失效, 不要再用.")
    return tokens


def call_responses(client, model: str, history: list, new_message: str,
                   round_no: int) -> dict:
    """调一次 Responses API (stream=True, store=False), 返回结构化回复 + 用量.

    注意: history 必须原地修改 (append), 不能用 history = history + [...]
    重新绑定 —— 重新绑定只改函数内局部变量, main 里的 history 收不到,
    后续轮次就没有前轮历史, 上下文延续是假的 (2026-11-17 Work GPT 复核发现)."""
    history.append({"role": "user", "content": new_message})
    print(f"\n--- 第 {round_no} 轮: 正在调 GPT ({model}) ---")
    t0 = time.time()
    stream = client.responses.create(
        model=model,
        instructions=INSTRUCTIONS,
        input=history,
        stream=True,
        store=False,
        text={"format": {"type": "json_schema", "name": "gpt_reply",
                         "schema": SCHEMA, "strict": True}},
    )
    output_parts: list[str] = []
    usage: dict | None = None
    actual_model: str | None = None
    completed = False
    total_chars = 0
    for event in stream:
        etype = getattr(event, "type", "")
        if etype == "response.output_text.delta":
            delta = getattr(event, "delta", "")
            total_chars += len(delta)
            if total_chars > 200000:  # 有界响应: 超限直接拒绝, 不无限攒
                raise RuntimeError("GPT 回复超过 200000 字符上限, 已丢弃")
            output_parts.append(delta)
        elif etype == "response.completed":
            completed = True
            resp = getattr(event, "response", None)
            if resp is not None:
                # 实际返回模型以服务端为准, 不拿请求参数冒充.
                actual_model = getattr(resp, "model", None) or actual_model
                u = getattr(resp, "usage", None)
                if u is not None:
                    # 用量缺失记 None, 不补 0 (2026-11-17 复核发现).
                    usage = {"input_tokens": getattr(u, "input_tokens", None),
                             "output_tokens": getattr(u, "output_tokens", None)}
        elif etype == "response.failed":
            resp = getattr(event, "response", None)
            err = getattr(resp, "error", None) if resp is not None else None
            raise RuntimeError(f"GPT 返回失败: {err}")
    if not completed:
        # 流没走完就断了: 拒绝用半截回复冒充完成 (2026-11-17 Work 本地修订补齐).
        raise RuntimeError("流在 response.completed 之前结束, 回复不完整, 已丢弃")
    duration_ms = int((time.time() - t0) * 1000)
    text = "".join(output_parts)
    if not text.strip():
        raise RuntimeError("GPT 返回了空回复 (没有伪造任何内容)")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"GPT 回复不是合法 JSON, 已原样丢弃: {e}\n原文前 500 字: {text[:500]}") from e
    missing = [f for f in SCHEMA["required"] if f not in parsed]
    if missing:
        raise RuntimeError(f"GPT 回复缺字段 {missing}, 已丢弃")
    # 结构化字段类型校验: 布尔不能是字符串, 数组里不能混别的类型.
    if not isinstance(parsed["reply"], str):
        raise RuntimeError("GPT 回复 reply 不是字符串, 已丢弃")
    if (not isinstance(parsed["questions_for_muse"], list)
            or not all(isinstance(q, str) for q in parsed["questions_for_muse"])):
        raise RuntimeError("GPT 回复 questions_for_muse 不是字符串数组, 已丢弃")
    if not isinstance(parsed["needs_user_decision"], bool):
        raise RuntimeError("GPT 回复 needs_user_decision 不是布尔值, 已丢弃")
    if not isinstance(parsed["proposed_next_step"], str):
        raise RuntimeError("GPT 回复 proposed_next_step 不是字符串, 已丢弃")
    if parsed["status"] not in ("continue", "waiting_user", "done", "failed"):
        raise RuntimeError(f"GPT 回复 status 取值非法: {parsed['status']}, 已丢弃")
    result = {
        "round": round_no,
        "requested_model": model,
        "model": actual_model or model,  # 实际返回模型; 没拿到才回退请求值并在 usage_status 注明
        "model_source": "response.completed" if actual_model else "request_fallback",
        "reply": parsed["reply"],
        "questions_for_muse": parsed["questions_for_muse"],
        "needs_user_decision": parsed["needs_user_decision"],
        "proposed_next_step": parsed["proposed_next_step"],
        "status": parsed["status"],
        "usage": usage,  # 未返回用量时为 None, 不补 0
        "usage_status": "provided" if usage is not None else "not_provided",
        "duration_ms": duration_ms,
        "funding": "chatgpt-plus-plan-usage",
        "cost_usd": 0,  # 计划用量不走 API 计费, 记 0 并注明来源
    }
    print(f"第 {round_no} 轮完成, 耗时 {duration_ms}ms, 用量 {usage}")
    print(f"回复: {parsed['reply'][:500]}")
    print(f"需要 Muse 补充: {parsed['questions_for_muse']}")
    print(f"需要用户决策: {parsed['needs_user_decision']}, 状态: {parsed['status']}")
    history.append({"role": "assistant", "content": text})
    return result


def save_partial(results: list[dict], status: str, error: str | None = None,
                 model: str | None = None) -> None:
    """每轮后立刻写一次结果文件: 中途崩了也有阶段结果和错误记录
    (2026-11-17 复核发现: 旧版只在全部跑完才写, 中途异常直接退出丢记录)."""
    payload: dict = {"status": status, "rounds": results,
                     "note": "真实调用记录, 含用量与耗时, 不含任何 token"}
    if model:
        payload["model"] = model
    if error:
        payload["error"] = error
    RESULT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="SIWC 真实调用测试 v2")
    parser.add_argument("--refresh", action="store_true", help="加跑 token 刷新演示")
    parser.add_argument("--bad-model", action="store_true", help="加跑失败恢复演示 (错模型名)")
    parser.add_argument("--offline-demo", action="store_true",
                        help="加跑断网恢复演示 (第 2 轮后手动断网, 再恢复后用同一份历史续聊)")
    args = parser.parse_args()

    from openai import OpenAI

    tokens = load_tokens()
    model = tokens.get("model") or "gpt-6.1-terra"
    # max_retries=0: 失败明确报错不自动重试 (Work 本地修订补齐); timeout 有界.
    client = OpenAI(api_key=tokens["access_token"],
                    base_url="https://api.openai.com/v1",
                    max_retries=0, timeout=120)

    results: list[dict] = []
    history: list[dict] = []
    try:
        r1 = call_responses(client, model, history,
                            "帮我研究购买耳机和宿舍用品, 给初步建议: 先分档比较耳机, 再列宿舍用品清单. "
                            "补充条件: 耳机预算 500 元, 宿舍桌子 60cm 宽.",
                            round_no=1)
        results.append(r1)
        save_partial(results, "partial", model=model)
        # 第 2 轮故意不重述数字: 只提“刚才说的预算和桌子尺寸”, 让 GPT 自己复述.
        # 只有它回复里出现 500 和 60, 才算上下文真的延续上了
        # (旧版第 2 轮自己把 500/60 写进去了, 再检查数字等于自证, 2026-11-17 复核发现).
        r2 = call_responses(client, model, history,
                            "补充: 请按我刚才说的预算和桌子尺寸更新建议, "
                            "并说清楚上一轮哪些结论变了、哪些没变.",
                            round_no=2)
        results.append(r2)
        save_partial(results, "partial", model=model)
        # 上下文延续自查: 第二轮回复应提到预算或桌子尺寸, 否则标出来.
        if "500" not in r2["reply"] and "60" not in r2["reply"]:
            print("\n注意: 第二轮回复里没提到 500 元或 60cm, 上下文可能没延续上, "
                  "请把结果发给 Muse 看一下.", file=sys.stderr)
        else:
            print("\n上下文延续自查通过: 第二轮回复引用了预算/尺寸.")
    except Exception as e:  # noqa: BLE001 - 测试脚本: 任何失败都要留下记录再退出
        print(f"\n调用失败: {e}", file=sys.stderr)
        print("请把报错原文和 siwc_test_result.json 一起发给 Muse, 不要自己反复试.",
              file=sys.stderr)
        save_partial(results, "failed", error=str(e)[:500], model=model)
        raise SystemExit(1)

    # --- 断网恢复演示 (只在 --offline-demo 时跑) ---
    if args.offline_demo:
        print("\n--- 断网恢复演示 ---")
        print("现在请断开网络 (关 Wi-Fi / 拔网线 / 开飞行模式), 然后按回车.")
        input("断网后按回车继续: ")
        try:
            call_responses(client, model, history,
                           "断网测试: 这轮应该失败.", round_no="offline-probe")
            print("意外: 断网了居然还能调通, 请把结果发给 Muse.", file=sys.stderr)
            results.append({"round": "offline-probe", "expected_failure": False,
                            "note": "断网后调用仍成功, 网络可能没断干净"})
        except Exception as e:  # noqa: BLE001 - 这就是要演示的: 断网失败被捕获
            print(f"如预期断网失败并被捕获: {str(e)[:300]}")
            results.append({"round": "offline-probe", "error": str(e)[:300],
                            "expected_failure": True,
                            "note": "断网后调用失败, 历史还在内存里, 没丢"})
        print("\n现在请恢复网络, 确认能上网后按回车, 脚本会用同一份历史续聊第 3 轮.")
        input("恢复网络后按回车继续: ")

    # --- 第 3 轮: GPT 追问一个需用户决策的问题 ---
    try:
        r3 = call_responses(client, model, history,
                            "请针对上面的讨论, 提出一个需要用户做决策的问题 "
                            "(例如耳机选降噪款还是音质款、宿舍用品先买哪几件), "
                            "把问题写进 questions_for_muse, 并把 needs_user_decision 设为 true. "
                            "不要直接给最终结论, 先等用户决策.",
                            round_no=3)
        results.append(r3)
        # Muse 汇总提问链路自查: 第 3 轮应标需用户决策且确实提出了问题.
        if r3["needs_user_decision"] and r3["questions_for_muse"]:
            print("\n第 3 轮自查通过: GPT 提出了需用户决策的问题, "
                  "Muse 汇总后问用户的链路可以接上 (汇总与提问本身要等桥接通后验证).")
        else:
            print("\n注意: 第 3 轮没有标需用户决策、或没提出问题, "
                  "Muse 汇总提问的链路这次没验证上, 请把结果发给 Muse 看一下.",
                  file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"\n第 3 轮调用失败: {e}", file=sys.stderr)
        save_partial(results, "failed", error=str(e)[:500], model=model)
        raise SystemExit(1)

    if args.refresh:
        try:
            tokens = refresh_tokens(tokens)
            # max_retries=0、timeout 有界, 与首次创建一致.
            client = OpenAI(api_key=tokens["access_token"],
                            base_url="https://api.openai.com/v1",
                            max_retries=0, timeout=120)
            r4 = call_responses(client, model, history,
                                "刷新后确认: 请用一句话确认你还记得前面的预算和桌子尺寸.",
                                round_no=4)
            results.append(r4)
            save_partial(results, "partial", model=model)
        except Exception as e:  # noqa: BLE001
            print(f"\n刷新演示失败: {e}", file=sys.stderr)
            results.append({"round": "refresh-demo", "error": str(e)[:500]})
            save_partial(results, "partial", model=model)

    if args.bad_model:
        print("\n--- 失败恢复演示: 故意用不存在的模型名 ---")
        try:
            call_responses(client, "gpt-does-not-exist-123", history,
                           "这轮应该失败.", round_no=99)
            print("意外: 错模型名居然没报错, 请把结果发给 Muse.", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - 这就是要演示的: 错误被捕获, 不崩
            print(f"如预期报错并被捕获: {str(e)[:300]}")
            print("失败恢复演示通过: 错误明确、脚本没崩、token 文件不受影响.")
            results.append({"round": "bad-model-demo", "error": str(e)[:300],
                            "expected_failure": True})
            save_partial(results, "partial", model=model)

    save_partial(results, "complete", model=model)
    print(f"\n全部完成. 结果已保存到: {RESULT_FILE}")
    print("请把这个文件发给 Muse (直接发文件, 不要复制粘贴 token, 这里面没有 token).")
    in_vals = [r["usage"]["input_tokens"] for r in results
               if isinstance(r.get("usage"), dict) and r["usage"].get("input_tokens") is not None]
    out_vals = [r["usage"]["output_tokens"] for r in results
                if isinstance(r.get("usage"), dict) and r["usage"].get("output_tokens") is not None]
    if in_vals or out_vals:
        print(f"本次测试总用量: 输入 {sum(in_vals)} tokens, 输出 {sum(out_vals)} tokens, "
              f"费用 0 美元 (走 ChatGPT Plus 计划用量, 不走 API 计费).")
    else:
        print("本次测试用量未提供 (服务端没返回 usage, 不补 0).")


if __name__ == "__main__":
    main()

"""桥 8003 本地测试 (pytest + TestClient, 全程 STUB, 不调真实 API).

运行: BRIDGE_TOKEN=<临时token> python -m pytest tests/ -v
token 只存在环境变量, 不写进文件.
"""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("BRIDGE_TOKEN", "test-token-" + "x" * 32)
os.environ["BRIDGE_DB"] = str(Path(__file__).resolve().parent / "test-bridge.sqlite")
# 每次跑测试用干净库
p = Path(os.environ["BRIDGE_DB"])
for suffix in ("", "-wal", "-shm"):
    try:
        Path(str(p) + suffix).unlink()
    except FileNotFoundError:
        pass

from bridge.server import app  # noqa: E402

TOKEN = os.environ["BRIDGE_TOKEN"]
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = "/api/muse-gpt"

CREATE_BODY = {
    "title": "购买耳机和宿舍用品",
    "original_request": "帮我研究购买耳机和宿舍用品。",
    "user_context": "住宿舍的学生",
    "project_goal": "宿舍添置 + 耳机推荐, 第一轮真实验收",
    "first_message": "请按耳机和宿舍用品两类, 先给研究思路和需要我补充的信息。",
    "idempotency_key": "muse-test-0001",
}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_1_health(client):
    r = client.get(f"{BASE}/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["service"] == "muse-gpt-bridge"
    assert body["adapter"] == "stub" and body["gpt_reachable"] is False


def test_2_unauthorized(client):
    # 无凭证 / 错凭证调创建, 一律 401, 且不产生讨论
    r = client.post(f"{BASE}/discussions", json=CREATE_BODY)
    assert r.status_code == 401
    r = client.post(f"{BASE}/discussions", json=CREATE_BODY,
                    headers={"Authorization": "Bearer wrong-token-value"})
    assert r.status_code == 401
    r = client.get(f"{BASE}/discussions/mg-doesnotexist")
    assert r.status_code == 401  # 无凭证连不存在的讨论也 401, 不泄露存在性


def _create(client):
    r = client.post(f"{BASE}/discussions", json=CREATE_BODY, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


def test_3_create_returns_stub_reply(client):
    d = _create(client)
    assert d["discussion_id"].startswith("mg-")
    assert d["openai_conversation_id"].startswith("stub-")
    assert d["status"] in ("active", "waiting_user") and d["round"] == 1
    g = d["gpt_reply"]
    assert set(g) == {"reply", "questions_for_muse", "needs_user_decision",
                      "proposed_next_step", "status"}
    assert "STUB" in g["reply"] and "不是 GPT 的真实回复" in g["reply"]
    assert g["needs_user_decision"] is True
    assert d["cost"]["cost_cny"] == 0.0 and d["cost"]["funding"] == "stub"
    test_3_create_returns_stub_reply.discussion_id = d["discussion_id"]


def test_4_append_round_2_references_input(client):
    did = test_3_create_returns_stub_reply.discussion_id
    r = client.post(f"{BASE}/discussions/{did}/messages",
                    json={"role": "muse",
                          "content": "预算 500 元以内, 宿舍桌子 120cm, 耳机主要听网课和音乐。",
                          "idempotency_key": "muse-test-0002"},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["round"] == 2
    assert "500" in body["gpt_reply"]["reply"]  # stub 回显了追加内容


def test_5_idempotent_create_no_duplicate(client):
    before = client.get(
        f"{BASE}/discussions/{test_3_create_returns_stub_reply.discussion_id}",
        headers=AUTH).json()
    r = client.post(f"{BASE}/discussions", json=CREATE_BODY, headers=AUTH)
    assert r.status_code == 201
    body = r.json()
    assert body["discussion_id"] == test_3_create_returns_stub_reply.discussion_id
    assert body["idempotent_replay"] is True
    after = client.get(f"{BASE}/discussions/{body['discussion_id']}",
                       headers=AUTH).json()
    assert after["round"] == before["round"]  # 未新增轮次
    assert after["cost_total_cny"] == before["cost_total_cny"]  # 未重复记账
    assert len(after["messages"]) == len(before["messages"])


def test_6_history_complete_ordered(client):
    did = test_3_create_returns_stub_reply.discussion_id
    r = client.get(f"{BASE}/discussions/{did}", headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    seqs = [m["seq_no"] for m in d["messages"]]
    assert seqs == sorted(seqs) and len(seqs) == 4  # 2 轮 x (muse + gpt)
    roles = [m["role"] for m in d["messages"]]
    assert roles == ["muse", "gpt", "muse", "gpt"]
    assert d["pending_questions"] == []  # 第二轮已回答, 无待办问题


def test_7_close_then_append_409(client):
    did = test_3_create_returns_stub_reply.discussion_id
    r = client.post(f"{BASE}/discussions/{did}/close",
                    json={"reason": "Muse 判断讨论完成",
                          "final_report": "耳机推荐三款……宿舍用品清单……"},
                    headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "closed"
    r = client.post(f"{BASE}/discussions/{did}/messages",
                    json={"role": "muse", "content": "还想再问一句",
                          "idempotency_key": "muse-test-0003"},
                    headers=AUTH)
    assert r.status_code == 409

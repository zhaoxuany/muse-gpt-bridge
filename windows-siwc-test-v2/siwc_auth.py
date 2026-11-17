"""SIWC 授权助手 (Windows 本地用, 只跑在用户自己电脑上).

做一件事: 帮用户走完 Sign in with ChatGPT 的 OAuth 授权 (PKCE),
拿到 access_token + refresh_token + 发下来的 client_id, 存进本地 token 文件,
供 test_siwc.py 和桥的 SIWCAdapter 使用.

流程 (按 OpenAI SIWC 官方文档原文, 2026-11-17 直接读过并核对过):
- https://developers.openai.com/siwc/token-sharing-open-source/sign-in
- https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions
1. 生成安装标识 ext_agent_host_id (urn:uuid:..., 存本地, 跨重启复用).
2. 本地起 http://127.0.0.1:<端口>/auth/callback 监听器 (不能用 localhost),
   生成全新的 state、nonce、PKCE verifier.
3. 浏览器打开 https://auth.openai.com/api/accounts/authorize:
   首次注册发 client_id=dynamic_agent_client + agent_name_hint +
   ext_agent_host_id; 用户登录 ChatGPT、选 workspace、勾选允许使用
   计划用量 (scope 里必须有 chatgpt.tokens.use.direct).
   回调自动回到本地监听器, 带回授权码 + OpenAI 发的 client_id (形如 oaiapp_...).
   dynamic_agent_client 只是首次注册的入口名, 绝不能当 client ID 存.
4. 用 code + PKCE 换 token: POST form-encoded 到
   https://auth.openai.com/api/accounts/oauth/token
   (grant_type=authorization_code + 发下来的 client_id + code +
   code_verifier + 同一 redirect_uri + 同一 resource, 不用 client secret).
   access_token (1 小时) + refresh_token (30 天).

!!! 2026-11-17 更正 !!!
- 旧版脚本用的 /oauth/register 动态注册接口 + urn:ietf:wg:oauth:2.0:oob
  回调 + JSON 换 token, 与官方文档不符, 已全部改对.
  旧的 token 端点默认值 https://auth.openai.com/oauth/token 也是错的.
- token 文件统一路径: ~/.muse-gpt-bridge/siwc_tokens.json
  (Windows 与 Linux 同义; 可用环境变量 SIWC_TOKEN_FILE 覆盖).
  旧路径 ~/.config/muse-gpt-bridge/siwc_tokens.json 已废弃.

!!! ID token 签名校验 (2026-11-17 已补) !!!
- 本脚本用 PyJWT + OpenAI 的 JWKS 校验 ID token 签名 (RS256), 再核对
  aud / iss / exp / nonce / sub; 任一项不对或拿不到 JWKS 就拒绝保存 token.
  保存的记录里写明 "id_token_signature_verified": true.
  依赖: PyJWT==2.10.1 (见 requirements.txt).

token 文件只放用户自己 Windows 本地, 绝不上传 Box、绝不贴进聊天记录.

用法 (Windows PowerShell, 在 windows-siwc-test 目录下):
    python siwc_auth.py
按提示操作即可. 成功后会打印 token 文件位置和过期时间 (不打印 token 本身).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# --- 端点 (已核对: 2026-11-17 直接读官方文档原文) -----------------------
# 来源: https://developers.openai.com/siwc/token-sharing-open-source/sign-in
#       "Registration and sign-in" 一节.
AUTH_BASE = os.environ.get("SIWC_AUTH_BASE", "https://auth.openai.com")
AUTHORIZE_PATH = "/api/accounts/authorize"   # 浏览器授权页
TOKEN_PATH = "/api/accounts/oauth/token"     # 换 token / 刷新 token (form-encoded)
AGENT_NAME = "muse-gpt-bridge-local-test"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
RESOURCE = "https://api.openai.com/v1"

TOKEN_FILE = (Path(os.environ["SIWC_TOKEN_FILE"]) if os.environ.get("SIWC_TOKEN_FILE")
              else Path.home() / ".muse-gpt-bridge" / "siwc_tokens.json")
HOST_ID_FILE = Path.home() / ".muse-gpt-bridge" / "ext_agent_host_id.txt"


def _get_or_create_host_id() -> str:
    """安装标识: 跨重启复用, 只存本地."""
    if HOST_ID_FILE.exists():
        return HOST_ID_FILE.read_text(encoding="utf-8").strip()
    host_id = f"urn:uuid:{secrets.token_hex(16)}"
    HOST_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOST_ID_FILE.write_text(host_id, encoding="utf-8")
    return host_id


def _post_form(url: str, payload: dict) -> dict:
    """token 端点只接受 form-encoded, 不能发 JSON (官方文档原文)."""
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(payload).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"\n请求失败 HTTP {e.code}: {body[:500]}", file=sys.stderr)
        print("先停下来核对官方文档里的端点, 不要反复试.", file=sys.stderr)
        raise SystemExit(1) from e


def _decode_jwt_payload_unverified(token: str) -> dict:
    """只解 payload 做结构性检查, 不校验签名 (仅用于取 kid 等调试信息)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(raw).decode())
    except Exception:  # noqa: BLE001 - 解不出来就当没有
        return {}


def _verify_id_token(id_token: str, client_id: str, nonce: str) -> None:
    """JWKS 校验 ID token 签名 + 核对 aud/iss/exp/nonce/sub.
    任一项不对就拒绝 (fail-closed), 不保存 token.
    流程: 取 openid-configuration 里的 jwks_uri -> 取 JWKS -> 按 kid 挑公钥
    -> PyJWT RS256 校验签名、aud、iss、exp, 再单独核对 nonce 与 sub."""
    if not id_token:
        print("\n拒绝: 授权返回里没有 id_token, 无法校验身份, 不保存 token.",
              file=sys.stderr)
        raise SystemExit(1)
    try:
        import jwt
    except ImportError:
        print("\n拒绝: 缺少 PyJWT 依赖, 无法校验 ID token 签名. "
              "请先跑 pip install -r requirements.txt (PyJWT==2.10.1).",
              file=sys.stderr)
        raise SystemExit(1) from None
    try:
        with urllib.request.urlopen(
                AUTH_BASE + "/.well-known/openid-configuration", timeout=30) as resp:
            oidc = json.loads(resp.read().decode())
        jwks_uri = oidc.get("jwks_uri")
        issuer = oidc.get("issuer", "https://auth.openai.com")
        if not jwks_uri:
            raise ValueError("openid-configuration 里没有 jwks_uri")
        with urllib.request.urlopen(jwks_uri, timeout=30) as resp:
            jwks = json.loads(resp.read().decode())
        header = jwt.get_unverified_header(id_token)
        key = None
        for k in jwks.get("keys", []):
            if k.get("kid") == header.get("kid"):
                key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(k))
                break
        if key is None:
            raise ValueError("JWKS 里找不到匹配 kid 的公钥")
        claims = jwt.decode(id_token, key, algorithms=["RS256"],
                            audience=client_id, issuer=issuer)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - 任何校验失败都拒绝
        print(f"\n拒绝: ID token 签名校验失败 ({e}), 不保存 token.", file=sys.stderr)
        raise SystemExit(1) from e
    problems = []
    if claims.get("nonce") != nonce:
        problems.append("nonce 对不上本次授权")
    if not claims.get("sub"):
        problems.append("缺少 sub")
    if problems:
        print("\n拒绝: ID token 内容检查有问题: " + "; ".join(problems),
              file=sys.stderr)
        raise SystemExit(1)
    print("ID token 签名已用 JWKS 校验通过 (aud/iss/exp/nonce/sub 均对上).")


def _protect_token_dir() -> None:
    """凭证目录只让当前用户访问 (尽力而为, 失败只警告不中断授权).
    Windows: icacls 去掉继承权限, 只留当前用户完全控制.
    Linux: 目录 0700. token 文件本身写时已用原子替换."""
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            import subprocess
            user = os.environ.get("USERNAME", "")
            subprocess.run(
                ["icacls", str(TOKEN_FILE.parent), "/inheritance:r",
                 "/grant:r", f"{user}:(OI)(CI)F"],
                capture_output=True, timeout=30, check=False)
        else:
            os.chmod(TOKEN_FILE.parent, 0o700)
    except Exception as e:  # noqa: BLE001 - 加固失败不挡授权, 只警告
        print(f"警告: 凭证目录权限加固没做成 ({e}), token 文件仍只在本地, "
              "请检查文件夹共享设置.", file=sys.stderr)


def _save_tokens(data: dict, client_id: str, model: str, host_id: str,
                 nonce: str) -> None:
    import time
    # 先做 JWKS 签名校验, 不通过直接拒绝, 不保存任何东西.
    _verify_id_token(data.get("id_token") or "", client_id, nonce)
    record = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "client_id": client_id,
        "expires_at": int(time.time()) + int(data.get("expires_in", 3600)),
        "model": model,
        "ext_agent_host_id": host_id,
        "scopes": (data.get("scope") or "").split(),
        "id_token": data.get("id_token"),
        # ID token 签名已用 JWKS 校验 (见 _verify_id_token).
        "id_token_signature_verified": True,
    }
    _protect_token_dir()
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, TOKEN_FILE)
    print(f"\nToken 已保存到本地: {TOKEN_FILE}")
    print(f"access_token 有效期约 {data.get('expires_in', 3600) // 60} 分钟, "
          f"refresh_token 有效期 30 天 (刷新后会换新的, 重新起算).")
    print("注意: 这个文件里有你的登录凭证, 只留在你自己电脑上, 不要发给任何人.")


class _CallbackServer(HTTPServer):
    """本地回调用监听器: 只收一次授权回调, 不记任何 token 到日志."""
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.result: dict = {}
        self.got_result = threading.Event()

    def handle_callback(self, qs: dict):
        self.result = qs
        self.got_result.set()


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 不写访问日志
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if parsed.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            return
        self.server.handle_callback(qs)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "<html><body><h2>授权完成, 可以关掉这个页面, 回到黑色窗口继续.</h2>"
            "</body></html>".encode("utf-8"))


def main() -> None:
    print("=== SIWC 授权助手 (只在你自己 Windows 电脑上跑) ===\n")
    host_id = _get_or_create_host_id()
    print(f"安装标识 (存本地, 跨重启复用): {host_id}")

    # 已有注册时复用发下来的 client_id (再次授权, 不用 dynamic_agent_client).
    existing: dict = {}
    if TOKEN_FILE.exists():
        try:
            existing = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    saved_client_id = existing.get("client_id") if isinstance(existing, dict) else None
    saved_id_token = existing.get("id_token") if isinstance(existing, dict) else None

    # 本地回调监听器: 先起好再打开浏览器 (官方文档要求).
    # 端口 1455 优先 (官方示例), 被占就让系统分配空闲端口.
    try:
        server = _CallbackServer(("127.0.0.1", 1455), _CallbackHandler)
    except OSError:
        server = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/auth/callback"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    state = secrets.token_hex(16)
    nonce = secrets.token_hex(16)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    params = {
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "resource": RESOURCE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "ext_agent_host_id": host_id,
    }
    if saved_client_id:
        # 再次授权: 用保存的发下来的 client_id, 不发 agent_name_hint,
        # 有 id_token 就带上 id_token_hint (可跳过账号选择).
        params["client_id"] = saved_client_id
        if saved_id_token:
            params["id_token_hint"] = saved_id_token
        print("检测到已有注册, 用保存的 client_id 做再次授权.")
    else:
        # 首次注册: 入口 client_id + 应用名 + host ID (官方文档原文).
        params["client_id"] = "dynamic_agent_client"
        params["agent_name_hint"] = AGENT_NAME

    auth_url = AUTH_BASE + AUTHORIZE_PATH + "?" + urllib.parse.urlencode(params)
    print("\n正在打开浏览器做授权 (如果浏览器没自动打开, 把下面链接复制到地址栏):")
    print(f"\n{auth_url}\n")
    print("在打开的页面里: 登录你的 ChatGPT 账号 -> 选你的 workspace -> "
          "确认勾选了“允许使用计划用量” -> 点允许.")
    print("授权成功后浏览器会自动跳回本地页面, 显示“授权完成”, 不用你手动复制授权码.")
    webbrowser.open(auth_url)

    print("\n正在等浏览器回调 (最多 5 分钟, 超时请重新跑)...")
    try:
        if not server.got_result.wait(timeout=300):
            print("没等到回调 (超时或浏览器没跳回来), 请重新跑一遍.", file=sys.stderr)
            raise SystemExit(1)
    finally:
        server.shutdown()
        server.server_close()
    result = server.result
    if result.get("error"):
        print(f"授权被拒绝或出错: {result.get('error')}", file=sys.stderr)
        print("先停下来, 把情况发给 Muse, 不要反复试.", file=sys.stderr)
        raise SystemExit(1)
    if result.get("state") != state:
        print("回调的 state 对不上本次授权, 拒绝这次结果 (防 CSRF).", file=sys.stderr)
        raise SystemExit(1)
    code = result.get("code")
    issued_client_id = result.get("client_id") or saved_client_id
    if not code:
        print("回调里没有授权码, 请重新跑一遍.", file=sys.stderr)
        raise SystemExit(1)
    if not saved_client_id and not result.get("client_id"):
        print("首次注册的回调里没有发下来的 client_id, 注册视为未完成, 请重新跑.",
              file=sys.stderr)
        raise SystemExit(1)
    print("已拿到授权码, 发下来的 client_id 已保存 (不打印完整值).")

    # 用 code + PKCE 换 token (form-encoded, 不用 client secret).
    print("\n正在用授权码换 token...")
    model = input("本轮测试用的模型 (直接回车用 gpt-6.1-terra, 不要用 gpt-5 系列): ").strip()
    model = model or "gpt-6.1-terra"
    token = _post_form(AUTH_BASE + TOKEN_PATH, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": issued_client_id,
        "code_verifier": verifier,
        "resource": RESOURCE,
    })
    if "access_token" not in token:
        print(f"换 token 失败, 返回里没有 access_token: {str(token)[:300]}", file=sys.stderr)
        raise SystemExit(1)
    scope = token.get("scope", "")
    if "chatgpt.tokens.use.direct" not in scope:
        print("\n警告: 返回的 scope 里没有 chatgpt.tokens.use.direct, "
              "说明这次授权只登了录、没拿到计划用量权限.", file=sys.stderr)
        print("请重新跑一遍, 授权页上确认勾选了允许使用计划用量.", file=sys.stderr)
        raise SystemExit(1)
    _save_tokens(token, issued_client_id, model, host_id, nonce)
    print("\n授权完成. 接下来跑 python test_siwc.py 做真实调用测试.")


if __name__ == "__main__":
    main()

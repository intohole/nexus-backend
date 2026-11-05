"""r166 本地真链 E2E：org 声明低频复核——被移出成员 ≤复核TTL 内 org 归零。

教训 #842 口径：不是「DB 标志位已翻转」，而是「持 org token 的客户端在
复核窗内拿到校正后声明」。真实 UC 服务（隔离库+隔离端口 18979）+ 真实
HTTP + 真实 HS256 签发 + 生产同构 UserCenterSDK，零 mock。

运行：python3 tests/e2e_org_recheck_r166.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from nexus.uc_sdk.client import UserCenterSDK  # noqa: E402

BASE = "http://127.0.0.1:18979"
PORT = 18979
UC_SECRET = "e2e-recheck-secret"
ORG_RECHECK_TTL = 2.5
PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  | {detail}" if detail and not ok else ""))


def api(method: str, path: str, payload=None, token: str = "") -> dict:
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    body = None if payload is None else json.dumps(payload).encode()
    try:
        with urllib.request.urlopen(req, body, timeout=10) as r:
            return json_loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from e


def json_loads(raw: bytes) -> dict:
    import json
    return json.loads(raw.decode())


def start_uc(db_path: str) -> subprocess.Popen:
    subprocess.run(["pkill", "-f", f"uvicorn.*{PORT}"], capture_output=True)
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(REPO),
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "JWT_SECRET_KEY": UC_SECRET,
        "APP_DEBUG": "true",
        "ENVIRONMENT": "development",
        "CORS_ALLOW_ALL_ORIGINS": "true",
        "RATE_LIMIT_ENABLED": "false",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "usercenter_app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd="/Users/intoblack/remoteWork/usercenter", env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        time.sleep(0.5)
        if proc.poll() is not None:
            raise RuntimeError("UC 进程提前退出")
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            continue
    raise RuntimeError("UC 启动超时")


def main() -> int:
    stamp = str(int(time.time()) % 1000000)
    db_path = str(Path(tempfile.gettempdir()) / f"uc_recheck_e2e_{stamp}.db")
    proc = start_uc(db_path)
    try:
        run_flow(stamp)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        for suffix in ("", "-wal", "-shm"):
            Path(db_path + suffix).unlink(missing_ok=True)
    print(f"\n==== {len(PASSED)} passed / {len(FAILED)} failed ====")
    return 1 if FAILED else 0


def run_flow(stamp: str):
    suffix = f"rc{stamp}"
    owner_name, member_name = f"rc_o_{suffix}", f"rc_m_{suffix}"
    pwd = "Probe2026ok"

    api("POST", "/api/auth/register", {"username": owner_name, "password": pwd, "app_key": "admin"})
    api("POST", "/api/auth/register", {"username": member_name, "password": pwd, "app_key": "admin"})
    owner_tok = _login(owner_name, pwd)
    member_tok = _login(member_name, pwd)
    check("双号注册+登录", bool(owner_tok) and bool(member_tok))

    org = api("POST", "/api/organizations", {"name": f"复核E2E组{stamp}"}, token=owner_tok)
    org_id = org.get("data", {}).get("id")
    check("owner 建组织", bool(org_id), str(org)[:150])

    inv = api("POST", f"/api/organizations/{org_id}/members",
              {"identifier": member_name, "role": "member"}, token=owner_tok)
    check("username identifier 邀请成员", inv.get("success") is True, str(inv)[:150])

    me = api("GET", "/api/users/me", token=member_tok)
    member_uid = (me.get("data") or {}).get("id")
    check("成员 user_id 取得", bool(member_uid), str(me)[:150])

    enter = api("POST", f"/api/organizations/{org_id}/enter", token=member_tok)
    org_token = (enter.get("data") or {}).get("access_token") or ""
    check("成员 enter 组织换 token", bool(org_token), str(enter)[:150])

    sdk = UserCenterSDK(base_url=BASE, app_key="admin", app_secret="s", jwt_secret_key=UC_SECRET)
    sdk._ORG_RECHECK_TTL_S = ORG_RECHECK_TTL

    r1 = sdk_verify(sdk, org_token)
    check("本地验签+首次复核：org 声明在场", r1.get("org_id") == org_id, str({k: r1.get(k) for k in ('org_id', 'org_role')}))

    rm = api("DELETE", f"/api/organizations/{org_id}/members/{member_uid}", token=owner_tok)
    check("owner 移除成员", rm.get("success") is True, str(rm)[:150])

    r2 = sdk_verify(sdk, org_token)
    check(f"复核窗内(<{ORG_RECHECK_TTL}s)节流生效：旧 org 仍在（诚实窗）",
          r2.get("org_id") == org_id, str({k: r2.get(k) for k in ('org_id',)}))

    time.sleep(ORG_RECHECK_TTL + 1.0)
    r3 = sdk_verify(sdk, org_token)
    check("教训#842 口径：复核窗过期后 org 声明归零（≤TTL 收敛，非 24h）",
          r3.get("org_id") is None and r3.get("org_role") is None,
          str({k: r3.get(k) for k in ('org_id', 'org_role')}))

    import asyncio
    r4 = asyncio.run(sdk._verify_token_remote(org_token))
    check("远程权威端点双证：org=None", r4.get("success") is True and r4.get("org_id") is None,
          str({k: r4.get(k) for k in ('success', 'org_id')}))

    personal = _login(member_name, pwd)
    r5 = sdk_verify(sdk, personal)
    check("成员个人态 token：org=None 且不炸", r5.get("success") is True and r5.get("org_id") is None,
          str({k: r5.get(k) for k in ('success', 'org_id')}))


def _login(username: str, pwd: str) -> str:
    resp = api("POST", "/api/auth/login", {"username": username, "password": pwd, "app_key": "admin"})
    return (resp.get("data") or {}).get("access_token") or ""


def sdk_verify(sdk: UserCenterSDK, token: str) -> dict:
    import asyncio
    return asyncio.run(sdk.verify_token(token))


if __name__ == "__main__":
    sys.exit(main())

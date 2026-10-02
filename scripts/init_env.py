#!/usr/bin/env python
"""环境变量体检与密钥生成(首次部署 / 迁到新环境时先跑这一条)。

    uv run python scripts/init_env.py            # 只体检,报告缺什么(不改文件)
    uv run python scripts/init_env.py --write     # 自动生成缺失的密钥并写回 .env 文件
    uv run python scripts/init_env.py --write --env dev   # 指定环境(默认取 ZHILV_ENV,未设置则用 .env)
    uv run python scripts/init_env.py --verify    # 按服务自身链路验证 RSA 公钥真的能下发

能自动生成的(缺了就补,已有值一律不动):
  JWT_SECRET             登录令牌签名密钥
  CREDENTIAL_FERNET_KEY  凭证库主密钥(工具凭证 / 模型 Key / Quick BI 密钥都用它加密)
  RSA_PUBLIC_KEY         登录密码加密公钥(base64(PEM))—— 缺了登录页拿不到公钥,
  RSA_PRIVATE_KEY        对应私钥                        GET /api/admin/auth/public-key 直接 401
  CRON_TICK_TOKEN        定时任务心跳共享 token

必须人工填的(脚本只报缺,不猜):POSTGRES_HOST/PORT/DB/USER/PASSWORD。

注意:改完 .env 需要重启服务才生效;RSA 密钥对各环境独立,换了密钥前端需刷新页面重新取公钥。
"""
from __future__ import annotations

import argparse
import base64
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 必填(人工):缺了服务起不来
REQUIRED_MANUAL = ["POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"]
# 可自动生成
GENERATED = ["JWT_SECRET", "CREDENTIAL_FERNET_KEY", "RSA_PUBLIC_KEY", "RSA_PRIVATE_KEY", "CRON_TICK_TOKEN"]
# 可选(不配也能跑,只是相关功能不可用)
OPTIONAL = {
    "REDIS_HOST": "Redis(SSE 跨实例中转 / 限流),单实例可不配",
    "MINIO_ENDPOINT": "对象存储,向量知识原文件才用",
    "QUICKBI_USER_TOKEN": "Quick BI 智能小Q 环境变量连接(也可在页面里配连接)",
}


def env_file(env_name: str) -> Path:
    return ROOT / (f".env.{env_name}" if env_name else ".env")


def read_env(path: Path) -> dict[str, str]:
    """极简 .env 解析:KEY=VALUE,忽略注释与空行(与 python-dotenv 读取口径一致即可)。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def gen_rsa_pair() -> tuple[str, str]:
    """生成 2048 位 RSA 密钥对,返回 (公钥 base64, 私钥 base64)。

    后端 auth._load_rsa_keys 读的是 base64(PEM) —— 单行,正好塞进 .env。
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return (base64.b64encode(public_pem).decode(), base64.b64encode(private_pem).decode())


def generate(keys: list[str]) -> dict[str, str]:
    from cryptography.fernet import Fernet

    values: dict[str, str] = {}
    if "RSA_PUBLIC_KEY" in keys or "RSA_PRIVATE_KEY" in keys:
        pub, priv = gen_rsa_pair()
        values["RSA_PUBLIC_KEY"] = pub
        values["RSA_PRIVATE_KEY"] = priv
    for k in keys:
        if k in values:
            continue
        if k == "JWT_SECRET":
            values[k] = secrets.token_urlsafe(48)
        elif k == "CREDENTIAL_FERNET_KEY":
            values[k] = Fernet.generate_key().decode()
        elif k == "CRON_TICK_TOKEN":
            values[k] = secrets.token_urlsafe(24)
    return {k: v for k, v in values.items() if k in keys}


def write_back(path: Path, values: dict[str, str]) -> None:
    """把生成的值写回 .env:已存在的 KEY= 空值就地补全,不存在的追加到文件末尾。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    if remaining:
        lines.append("")
        lines.append("# —— scripts/init_env.py 自动生成 ——")
        lines.extend(f"{k}={v}" for k, v in remaining.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify_rsa() -> int:
    """走服务自身的 config + auth 链路验证:不是「.env 里有值」,而是「服务真的能用」。

    分开两件事很重要:文件里写了值 ≠ 服务读到了值(环境名不对、没重启、被进程环境变量
    覆盖,都会让前者成立而后者不成立)。登录页那句「获取加密公钥失败」对应的正是后者。
    """
    try:
        from cogni_agent.user_center_service import auth as auth_service
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 导入服务模块失败:{exc}")
        return 1
    try:
        pem = auth_service.get_public_key_pem()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 服务读不到 RSA 公钥({exc})—— GET /api/admin/auth/public-key 就会返回 401")
        print("  处理:uv run python scripts/init_env.py --write,然后**重启服务**")
        return 1
    if not pem.startswith("-----BEGIN"):
        print("✗ 公钥格式不对:应为 base64(PEM),解出来要以 -----BEGIN 开头")
        return 1
    import base64 as _b64
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    probe = "init-env-verify-" + secrets.token_hex(4)
    pub = serialization.load_pem_public_key(pem.encode(), backend=default_backend())
    cipher = pub.encrypt(probe.encode(), padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    try:
        back = auth_service.decrypt_password(_b64.b64encode(cipher).decode())
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 私钥解密失败({exc})—— 公私钥不配对,请 --rotate RSA_PUBLIC_KEY RSA_PRIVATE_KEY 重新生成一对")
        return 1
    if back != probe:
        print("✗ 解密结果与原文不一致,公私钥不是同一对")
        return 1
    print("✓ RSA 密钥对可用:公钥可下发、加解密往返一致(登录页取公钥不会再 401)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="环境变量体检与密钥生成")
    # 默认必须与 cogni_agent/config.py 的加载口径**完全一致**:ZHILV_ENV 没设 => 读 .env。
    # 曾经这里默认 "local",于是密钥被写进 .env.local,而服务读的是 .env ——
    # 脚本报「已生成」,登录页却照旧 401,排查成本极高。默认值不一致本身就是 bug。
    ap.add_argument("--env", default=os.environ.get("ZHILV_ENV", ""),
                    help="环境名(local/dev/prod),对应 .env.{env};默认取 ZHILV_ENV,未设置则用 .env")
    ap.add_argument("--write", action="store_true", help="生成缺失的密钥并写回 .env 文件")
    ap.add_argument("--verify", action="store_true",
                    help="按服务自身的配置加载链路验证 RSA 密钥真的可用(公钥下发 + 加解密往返)")
    ap.add_argument("--rotate", nargs="*", metavar="KEY",
                    help="强制重新生成指定密钥(如 --rotate RSA_PUBLIC_KEY RSA_PRIVATE_KEY);"
                         "注意:轮换 CREDENTIAL_FERNET_KEY 会让已加密的凭证全部解不开")
    args = ap.parse_args()

    if args.verify:
        return verify_rsa()

    path = env_file(args.env)
    env = read_env(path)
    # 已 export 到进程环境的值同样算「已配置」(容器部署常见)
    merged = {k: (env.get(k) or os.environ.get(k, "")) for k in REQUIRED_MANUAL + GENERATED + list(OPTIONAL)}

    print(f"环境文件 : {path}{'(不存在)' if not path.exists() else ''}")
    app_path = env_file(os.environ.get("ZHILV_ENV", ""))
    print(f"服务读取 : {app_path.name}(由 ZHILV_ENV 决定,当前 ZHILV_ENV="
          f"{os.environ.get('ZHILV_ENV') or '未设置'})")
    if app_path != path:
        print(f"  ⚠ 写入的文件不是服务会读的文件:密钥写进 {path.name} 也不会生效。")
        print(f"    要么去掉 --env(写 {app_path.name}),要么启动服务时 export ZHILV_ENV={args.env}")
    missing_manual = [k for k in REQUIRED_MANUAL if not merged.get(k)]
    missing_gen = [k for k in GENERATED if not merged.get(k)]
    rotate = [k for k in (args.rotate or []) if k in GENERATED]

    def source_of(k: str) -> str:
        if env.get(k):
            return ""
        return "(来自进程环境变量)" if os.environ.get(k) else ""

    for k in REQUIRED_MANUAL:
        print(f"  {'✓' if merged.get(k) else '✗'} {k}{source_of(k)}"
              f"{'' if merged.get(k) else '  ← 必填,请手工填写数据库连接'}")
    for k in GENERATED:
        mark = "✓" if merged.get(k) else "✗"
        note = "" if merged.get(k) else "  ← 缺失" + ("(登录页会 401:拿不到 RSA 公钥)" if k.startswith("RSA") else "")
        print(f"  {mark} {k}{source_of(k)}{note}")
    for k, why in OPTIONAL.items():
        print(f"  {'✓' if merged.get(k) else '·'} {k}(可选:{why})")

    todo = sorted(set(missing_gen) | set(rotate))
    if not todo:
        print("\n✓ 需要的密钥都已配置" + ("" if not missing_manual else f";但仍缺人工项:{', '.join(missing_manual)}"))
        return 0 if not missing_manual else 2

    if not args.write:
        print(f"\n缺少 {len(todo)} 个密钥:{', '.join(todo)}")
        print("  生成并写入:uv run python scripts/init_env.py --write" + (f" --env {args.env}" if args.env else ""))
        return 2

    if not path.exists():
        example = ROOT / ".env.example"
        path.write_text(example.read_text(encoding="utf-8") if example.exists() else "", encoding="utf-8")
        print(f"  · 已按 .env.example 创建 {path.name}")
    values = generate(todo)
    write_back(path, values)
    print(f"\n✓ 已生成并写入 {len(values)} 个密钥:{', '.join(sorted(values))}")
    if "CREDENTIAL_FERNET_KEY" in rotate:
        print("  ⚠ 轮换了 CREDENTIAL_FERNET_KEY:此前加密保存的工具凭证 / 模型 Key / Quick BI 密钥都需要重新录入")
    if missing_manual:
        print(f"  ⚠ 仍缺人工项:{', '.join(missing_manual)}(数据库连接必须自己填)")
        return 2
    print("  下一步:uv run python scripts/init_db.py(改完 .env 需重启服务才生效)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

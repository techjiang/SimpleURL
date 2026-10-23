"""口令哈希与安全随机。

- `hash_secret` / `verify_secret`：管理密钥的存储与校验。优先 Argon2id
  （内存硬化，抗 GPU / ASIC 暴力破解），环境缺少 argon2-cffi 时自动退回
  PBKDF2-HMAC-SHA256，两种编码格式互不兼容但接口一致。
- `sign`：HMAC-SHA256 签名，用于会话令牌。
- `random_token`：安全随机串，用于 nonce 等。

 argon2 与 pbkdf2 的编码都以算法名开头，因此配置文件里可以直接放哈希值，
不必再存明文密钥。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

try:  # Argon2id 是首选，装不上就退回 PBKDF2
    from argon2.exceptions import VerificationError
    from argon2.low_level import Type, hash_secret as _argon2_hash, verify_secret as _argon2_verify

    _HAS_ARGON2 = True
except Exception:  # pragma: no cover - 只在缺依赖时走到
    _HAS_ARGON2 = False

ARGON2_PREFIX = "argon2id"
# argon2 的 PHC 编码带前导 $，如 $argon2id$v=19$...
PBKDF2_PREFIX = "pbkdf2_sha256"

# Argon2id 参数：OWASP 建议的 m=19MiB / t=2 / p=1
_ARGON2_TIME = 2
_ARGON2_MEMORY_KIB = 19 * 1024
_ARGON2_PARALLELISM = 1
_HASH_LEN = 32
_SALT_LEN = 16

# PBKDF2 兜底参数：60 万次迭代，SHA-256
_PBKDF2_ROUNDS = 600_000


def random_token(nbytes: int = 32) -> str:
    """URL 安全的随机串，默认 256 bit。"""
    return secrets.token_urlsafe(nbytes)


def sign(secret: str, value: str) -> str:
    """HMAC-SHA256，恒定时间比较由调用方负责。"""
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.b64decode(text + pad)


def is_hash(value: str | None) -> bool:
    """是否已经是本模块产出的哈希编码。"""
    if not value:
        return False
    # argon2 走 PHC 标准编码，带前导 $；PBKDF2 用自定义的可读前缀
    return value.startswith(f"${ARGON2_PREFIX}$") or value.startswith(f"{PBKDF2_PREFIX}$")


def _hash_pbkdf2(secret: bytes, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", secret, salt, _PBKDF2_ROUNDS, _HASH_LEN)
    return f"{PBKDF2_PREFIX}$i={_PBKDF2_ROUNDS}${_b64encode(salt)}${_b64encode(digest)}"


def _hash_argon2(secret: bytes, salt: bytes) -> str:
    # argon2 的 low_level 接口直接产出 PHC 标准编码字符串
    return _argon2_hash(
        secret,
        salt,
        time_cost=_ARGON2_TIME,
        memory_cost=_ARGON2_MEMORY_KIB,
        parallelism=_ARGON2_PARALLELISM,
        hash_len=_HASH_LEN,
        type=Type.ID,
    ).decode("ascii")


def hash_secret(secret: str) -> str:
    """把明文密钥变成可存储的哈希编码（每次调用盐都不同）。"""
    if not secret:
        return ""
    raw = secret.encode("utf-8")
    salt = secrets.token_bytes(_SALT_LEN)
    if _HAS_ARGON2:
        return _hash_argon2(raw, salt)
    return _hash_pbkdf2(raw, salt)


def _parse(encoded: str) -> tuple[str, dict, str, str]:
    """拆开算法 / 参数 / 盐 / 摘要。argon2 的 PHC 编码开头多一个 $。"""
    if encoded.startswith("$"):
        encoded = encoded[1:]
    algo, rest = encoded.split("$", 1)
    parts = rest.split("$")
    # argon2: v=19, m=..,t=..,p=.., salt, hash；PBKDF2: i=.., salt, hash
    params_b64, salt_b64, hash_b64 = parts[-3], parts[-2], parts[-1]
    params = {}
    if params_b64:
        for part in params_b64.split(","):
            key, _, value = part.partition("=")
            if key:
                params[key] = value
    return algo, params, salt_b64, hash_b64


def verify_secret(secret: str | None, encoded: str | None) -> bool:
    """校验明文密钥是否匹配哈希编码。任何异常都算不匹配。"""
    if not secret or not encoded or not is_hash(encoded):
        return False
    try:
        algo, params, salt_b64, hash_b64 = _parse(encoded)
        salt = _b64decode(salt_b64)
        raw = secret.encode("utf-8")
        if algo == ARGON2_PREFIX:
            if not _HAS_ARGON2:
                return False
            try:
                # 参数都在 PHC 编码里，verify 只需要完整编码
                _argon2_verify(encoded.encode("ascii"), raw, Type.ID)
            except VerificationError:
                return False
            return True
        if algo == PBKDF2_PREFIX:
            rounds = int(params.get("i", _PBKDF2_ROUNDS))
            expected = hashlib.pbkdf2_hmac("sha256", raw, salt, rounds, _HASH_LEN)
            return hmac.compare_digest(expected, _b64decode(hash_b64))
    except Exception:
        return False
    return False


def needs_rehash(encoded: str | None) -> bool:
    """哈希是否该升级：算法降级或参数低于当前标准。"""
    if not encoded or not is_hash(encoded):
        return True
    try:
        algo, params, _, _ = _parse(encoded)
    except Exception:
        return True
    if algo == PBKDF2_PREFIX:
        return _HAS_ARGON2 or int(params.get("i", 0)) < _PBKDF2_ROUNDS
    if algo == ARGON2_PREFIX:
        return (
            int(params.get("t", 0)) != _ARGON2_TIME
            or int(params.get("m", 0)) != _ARGON2_MEMORY_KIB
            or int(params.get("p", 0)) != _ARGON2_PARALLELISM
        )
    return True


def fingerprint(value: str) -> str:
    """密钥指纹：稳定、短、不可反推原值，用于会话绑定与配置展示。"""
    return hashlib.sha256(f"fp::{value}".encode()).hexdigest()[:16]


def compare_digest(a: str | None, b: str | None) -> bool:
    """恒定时间字符串比对，长度不等也走同一条路径。"""
    if a is None or b is None:
        return False
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))

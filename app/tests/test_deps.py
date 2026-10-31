"""依赖清单的验收线：装到干净机器上不能少东西。

这类问题在写代码的机器上永远看不到 —— 那个 env 里早被别的包捎带装过，
CI 建的镜像也一样。只有用户第一次 `pip install -r requirements.txt` 才会撞上。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1].parent
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()

# 这些库由其它依赖带进来，不必单列
PROVIDED_BY = {"starlette": "fastapi", "pydantic_core": "pydantic", "anyio": "fastapi"}

# 明确可选、缺了会自动降级的依赖：不当成「漏写」
OPTIONAL = {"argon2-cffi"}

# import 名和发行包名不一致的那几个
DISTRIBUTION_NAME = {
    "dotenv": "python-dotenv",
    "multipart": "python-multipart",
    "jose": "python-jose",
    "yaml": "pyyaml",
    "PIL": "pillow",
    "magic": "python-magic",
    # 有意不装：crypto 里 try/except 退回 PBKDF2，属可选加速项
    "argon2": "argon2-cffi",
    "jwt": "pyjwt",
}


class TestRequirements:
    def test_form_parsing_dependency_is_declared(self):
        """`await request.form()` 要 python-multipart。

        YOURLS 兼容接口的中间件把缺失异常吞了，界面上一句「缺少 url 参数」——
        客户端发的整份表单静默消失。清单里没这个包，装到干净机器上就是这样。
        """
        assert "python-multipart" in REQUIREMENTS

    def test_multipart_is_importable(self):
        import python_multipart  # noqa: F401

    def test_every_imported_library_is_declared(self):
        """扫一遍 app/ 的顶层 import，第三方库都要在清单里出现。"""
        stdlib = set(sys.stdlib_module_names)
        local = {p.stem for p in (ROOT / "app").rglob("*.py")} | {"app", "core", "tests"}
        missing = set()
        for path in _py_files(ROOT / "app"):
            for mod in _imports(path):
                if mod in stdlib or mod in local:
                    continue
                dist = DISTRIBUTION_NAME.get(mod, mod).lower()
                if dist in PROVIDED_BY:
                    owner = PROVIDED_BY[dist]
                    assert owner in REQUIREMENTS, f"{mod} 由 {owner} 提供，但 {owner} 不在清单里"
                    continue
                if dist in OPTIONAL:
                    continue
                if dist not in REQUIREMENTS:
                    missing.add(dist)
        assert not missing, f"这些库被 import 了，requirements.txt 里却没有：{sorted(missing)}"


def _imports(path: Path) -> set[str]:
    src = path.read_text(encoding="utf-8")
    mods = set(re.findall(r"^\s*import\s+([a-zA-Z_][\w]*)", src, re.M))
    mods |= set(re.findall(r"^\s*from\s+([a-zA-Z_][\w]*)", src, re.M))
    return mods


def _py_files(root: Path):
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        yield path


def _noop():  # pragma: no cover - 占位，保持 import 顺序清晰
    return None

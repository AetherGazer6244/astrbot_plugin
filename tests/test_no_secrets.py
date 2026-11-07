"""仓库防泄漏守卫：真实凭据绝不能进版本库。

背景（真实事故）：本仓库曾经把 AstrBot WebUI 的**真实登录密码**硬编码在
tests/test_extract_cookie.py 里，随 commit push 到了**公开**的 GitHub 仓库；
更早的 tools/extract_cookie.py 版本里还写着**服务器 IP + 账号**。
三样凑齐 = 任何翻到仓库的人都能登进后台改配置、读数据。

这个文件是防再犯的闸门，分两层：

1. **形状规则**（全仓文本）：命中即失败——
   - AstrBot OpenAPI Key（``abk_...``）
   - JWT（``eyJ...``）
   - 长度像真的学校会话（``JSESSIONID=`` + ≥16 位随机值）
   - 公网 IPv4（环回/私网/链路本地/文档保留段除外）
   - ``ASTRBOT_PASS=字面量``
   - ``_conf_schema.json`` 里 ``secret: true`` 的字段必须默认值为空
2. **疑似口令白名单**（Python 字面量）：形似口令的字符串字面量必须登记在
   ``ALLOWED_FAKE_SECRETS`` 里并注明理由。新出现的可疑字面量没登记 → 失败。

用法：写测试用的假值时，把它加进白名单并写清「为什么是假的」；
**真凭据永远不许加进白名单**——那等于把洞重新打开。
"""

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 需要扫描的文本后缀（二进制/图片/字体一律跳过）
TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".txt", ".js", ".ts",
                 ".html", ".css", ".bat", ".sh", ".toml", ".cfg", ".ini", ".env",
                 ".xml", ".csv"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv",
             "venv", "node_modules", ".idea", ".vscode"}
MAX_FILE_BYTES = 500_000

# ---------------- 第一层：形状规则 ----------------

SHAPE_RULES = [
    ("AstrBot OpenAPI Key", re.compile(r"\babk_[A-Za-z0-9]{8,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{20,}")),
    ("真实学校会话 Cookie", re.compile(r"JSESSIONID=[A-Za-z0-9]{16,}")),
    ("ASTRBOT_PASS 明文赋值", re.compile(r"ASTRBOT_PASS\s*=\s*[\"'][^\"']+[\"']")),
]

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _is_public_ipv4(ip: str) -> bool:
    """判断是否公网可路由地址（环回/私网/链路本地/组播/文档保留段放行）。"""
    try:
        parts = [int(x) for x in ip.split(".")]
    except ValueError:
        return False
    if len(parts) != 4 or any(p > 255 for p in parts):
        return False
    a, b = parts[0], parts[1]
    if a == 0 or a == 127 or a >= 224:
        return False
    if a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168):
        return False
    if a == 169 and b == 254:
        return False
    # RFC 5737 文档保留段（示例地址）不算公网
    return not (a == 203 and b == 0 and parts[2] == 113)


# ---------------- 第二层：疑似口令字面量 ----------------

# 形似口令：长度 ≥8、至少 3 个字母 + 1 个数字、只含「密码常见符号」
_SECRET_LITERAL_RE = re.compile(r"^[A-Za-z0-9@!#$%^&*()+=._-]{8,}$")

# 明显不是口令的常见字面量（编码名之类），显式豁免免得每次都要加白名单
BENIGN_LITERALS = {"utf-8", "utf-8-sig", "ascii", "latin-1", "base64"}

# 已登记的**假值**。格式：值 -> 理由（理由会出现在失败信息里，提醒下一个人）
ALLOWED_FAKE_SECRETS = {
    "TestPass1": "测试用的假密码（占位，非任何真实系统口令）",
    "SECRET_SESSION_VALUE_0123456789": "测试用的假 JSESSIONID 值（非真实会话）",
}


def _iter_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            # utf-8-sig：吃掉 Windows 编辑器可能写下的 BOM，否则 AST 解析直接失败
            yield path, path.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            continue


def test_repo_has_no_credential_shaped_strings():
    """全仓不允许出现凭据形状的字符串。"""
    problems = []
    for path, text in _iter_text_files():
        rel = path.relative_to(ROOT).as_posix()
        for label, pattern in SHAPE_RULES:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{rel}:{line} 命中「{label}」：{m.group()[:24]}…")
        for m in _IPV4_RE.finditer(text):
            if _is_public_ipv4(m.group()):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{rel}:{line} 命中「公网 IP 地址」：{m.group()}")
    assert not problems, "仓库里出现疑似真实凭据：\n" + "\n".join(problems)


def test_schema_secret_fields_have_empty_defaults():
    """配置 schema 里标了 secret 的字段，默认值必须为空。"""
    schema_path = ROOT / "_conf_schema.json"
    if not schema_path.exists():
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    bad = [
        f"{key}（secret 字段默认值不为空）"
        for key, spec in schema.items()
        if isinstance(spec, dict)
        and spec.get("secret") is True
        and str(spec.get("default", "") or "").strip()
    ]
    assert not bad, "这些 secret 字段带了非空默认值：\n" + "\n".join(bad)


def test_credential_like_literals_are_registered():
    """形似口令的字符串字面量必须登记在白名单里（防止真值被顺手贴进来）。"""
    problems = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value
            if not _SECRET_LITERAL_RE.match(text):
                continue
            if text in BENIGN_LITERALS:
                continue
            letters = sum(c.isalpha() for c in text)
            digits = sum(c.isdigit() for c in text)
            if letters < 3 or digits < 1:
                continue
            # 形如 usage_24h / recharged_24h 的小写下划线标识符是字典键，不是口令
            if "_" in text and text.islower():
                continue
            if text in ALLOWED_FAKE_SECRETS:
                continue
            problems.append(f"{rel}:{node.lineno} 未登记的可疑字面量：{text!r}")
    assert not problems, (
        "以下字面量形似口令但没登记在 ALLOWED_FAKE_SECRETS 里：\n"
        + "\n".join(problems)
        + "\n\n若是测试用的假值 → 加进白名单并写明理由；"
        "若疑似真凭据 → 立刻换掉，并检查它有没有被 push 出去过。"
    )


def test_allowed_fake_secrets_are_still_dummies():
    """白名单本身不能被塞进真值——用形状规则反向兜底。"""
    for value, reason in ALLOWED_FAKE_SECRETS.items():
        assert reason.strip(), f"白名单项 {value!r} 必须写明「为什么是假的」"
        assert " " not in value, f"白名单项不该含空格：{value!r}"
        assert not value.startswith("abk_"), f"白名单项不该是 API Key：{value!r}"
        assert not value.startswith("eyJ"), f"白名单项不该是 JWT：{value!r}"
        assert not _is_public_ipv4(value), f"白名单项不该是公网 IP：{value!r}"

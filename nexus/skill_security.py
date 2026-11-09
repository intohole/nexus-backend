"""技能内容安全红线扫描器（舰队单源）：OWASP AST01 红旗词面+结构规则，零 LLM。

r86 汰换定谳：scripts/skill_security_scan.py（S58 源）与 nexus-agent 仓内副本的双副本
同步纪律退役，规则演进只在本模块。扫描对象=单个技能目录（SKILL.md frontmatter+正文+
子目录文本文件，深度≤3）。severity：critical=远程内容管道执行/base64 解码管道；
high=frontmatter 藏 base64+非 HTTPS 下载+写身份工件+外传凭据意图+注入指令；
medium=Prerequisites 安装命令/社工话术/typosquatting/写上下文注入文件。
blocked 判据=存在 critical 或 high（与 S58 fail 判据对齐）。
R1 证据启发（r85 守底判例，教训#190）：≥40 字母数字串须带 base64 证据（=padding 或
大小写+数字混合）才判，排除 40-hex commit sha/长 slug 假阳性类。
消费方：nexus.skills 安检闸默认扫描器/growth 转正闸/scripts 薄 CLI（exempt 审计层）。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

IDENTITY_ARTIFACTS = ["MEMORY.md", "SOUL.md", "persona"]
CONTEXT_ARTIFACTS = ["CLAUDE.md", "AGENTS.md"]
LOCAL_HOSTS = re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0|::1|\[::1\])")
B64_BLOB = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
B64_DECODE_PIPE = re.compile(r"(base64\s+-d|base64\s+--decode|openssl\s+enc.*-d)", re.I)
DOWNLOAD = re.compile(r"\b(?:curl|wget)\b")
PLAIN_HTTP = re.compile(r"http://(?!localhost|127\.0\.0\.1|0\.0\.0\.0)")
PROXY_HTTP = re.compile(r"(?:-x\s+|--proxy(?:=\s*|\s+))http://\S+")
URL_ANY = re.compile(r"https?://\S+")
PIPE_EXEC = re.compile(r"\|\s*(?:sudo\s+)?(?:sh|bash|zsh|python3?|perl)\b|\b(?:sh|bash)\s*<\(")
EXFIL_HINT = re.compile(
    r"\b(?:~/.ssh|id_rsa|id_ed25519|\.aws/credentials|\.netrc|wallet|metamask|keychain|cookie[sz]?\b)", re.I
)
WRITE_TARGET = re.compile(
    r"(?:>>?\s*|\bcp\s+\S+\s+|\bmv\s+\S+\s+|\btee\s+(?:-a\s+)?|\brm\s+(?:-\w+\s+)*|\bappend\b[^.\n]{0,30}(?:to|进|到)|写入)"
)
TYPOSQUAT_WORDS = re.compile(r"\b(?:gogle|googel|microsooft|cloudfare|faceb00k|gitlhub|npmjs-com|pypi-org)\b", re.I)
SOCIAL_EN = re.compile(r"(?:run this command to (?:enable|activate)|execute the following to unlock|you must (?:first )?run)", re.I)
SOCIAL_ZH = re.compile(r"(?:运行此命令以(?:启用|开启|激活)|必须先运行|执行以下命令以解锁)")
PREREQ_HEADING = re.compile(r"^#{1,3}\s*(?:Prerequisites|前置条件?|预备)", re.M | re.I)
INSTALL_CMD = re.compile(
    r"\b(?:curl|wget|pip3?\s+install|npm\s+install|brew\s+install|apt(?:-get)?\s+install|sh\s+-c|bash\s)", re.I
)
INJECTION_INSTRUCTIONS = re.compile(
    r"(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above)\s+instructions", re.I
)
TEXT_SUFFIX = {".md", ".txt", ".py", ".js", ".ts", ".sh", ".json", ".yaml", ".yml", ".toml", ""}

RULE_SOURCES = {
    "R1": "AST01: YAML注释/正文藏base64；Snyk ToxicSkills「三行markdown外传SSH密钥」",
    "R2": "AST01: 非HTTPS下载指令（中间人替换安装源）",
    "R3": "AST01: MEMORY.md投毒/SOUL.md持久化后门（身份工件保护）",
    "R4": "AST01: typosquatting品牌变形（gogle-workspace判例）",
    "R5": "AST01: Prerequisites段社工诱导跑安装命令",
    "R6": "AST01: 社工话术（运行此命令以启用…）+提示词注入忽略指令",
    "R7": "AST01/Snyk: 远程内容管道执行（curl|sh）；ClawHavoc AMOS投递形态",
    "R8": "AST01: 过度权限请求——写上下文注入文件（CSA Agent Context Poisoning）",
    "R9": "Snyk/USENIX: 外传凭据意图（.ssh/.aws/wallet/keychain词面）",
}

__all__ = ["Finding", "has_b64_evidence", "scan_text", "scan_skill_dir"]


@dataclass
class Finding:
    rule: str
    severity: str
    file: str
    line: int
    snippet: str
    source: str


def has_b64_evidence(blob: str) -> bool:
    if "=" in blob:
        return True
    return bool(re.search(r"[A-Z]", blob)) and bool(re.search(r"[a-z]", blob)) and bool(re.search(r"[0-9]", blob))


def scan_text(rel: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    lines = text.splitlines()
    in_frontmatter = False
    prereq_zone = False
    for i, line in enumerate(lines, 1):
        if i == 1 and line.strip() == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if line.strip() == "---":
                in_frontmatter = False
            blob = B64_BLOB.search(URL_ANY.sub("", line))
            if blob and has_b64_evidence(blob.group(0)):
                out.append(Finding("R1", "high", rel, i, line.strip()[:120], RULE_SOURCES["R1"]))
            continue
        if PREREQ_HEADING.match(line):
            prereq_zone = True
        elif re.match(r"^#{1,3}\s", line):
            prereq_zone = False
        hit_dl = DOWNLOAD.search(line)
        if hit_dl:
            non_proxy_line = PROXY_HTTP.sub(" ", line)
            if PLAIN_HTTP.search(non_proxy_line) and not LOCAL_HOSTS.search(non_proxy_line):
                out.append(Finding("R2", "high", rel, i, line.strip()[:120], RULE_SOURCES["R2"]))
            if PIPE_EXEC.search(line):
                out.append(Finding("R7", "critical", rel, i, line.strip()[:120], RULE_SOURCES["R7"]))
            blob = B64_BLOB.search(URL_ANY.sub("", line))
            if B64_DECODE_PIPE.search(line) or (blob and has_b64_evidence(blob.group(0)) and PIPE_EXEC.search(line)):
                out.append(Finding("R1", "critical", rel, i, line.strip()[:120], RULE_SOURCES["R1"]))
        for artifact in IDENTITY_ARTIFACTS:
            if artifact.lower() in line.lower():
                if WRITE_TARGET.search(line):
                    out.append(Finding("R3", "high", rel, i, line.strip()[:120], RULE_SOURCES["R3"]))
                elif EXFIL_HINT.search(line):
                    out.append(Finding("R9", "high", rel, i, line.strip()[:120], RULE_SOURCES["R9"]))
        for artifact in CONTEXT_ARTIFACTS:
            if artifact in line and WRITE_TARGET.search(line):
                out.append(Finding("R8", "medium", rel, i, line.strip()[:120], RULE_SOURCES["R8"]))
        if TYPOSQUAT_WORDS.search(line):
            out.append(Finding("R4", "medium", rel, i, line.strip()[:120], RULE_SOURCES["R4"]))
        if SOCIAL_EN.search(line) or SOCIAL_ZH.search(line):
            out.append(Finding("R6", "medium", rel, i, line.strip()[:120], RULE_SOURCES["R6"]))
        if INJECTION_INSTRUCTIONS.search(line):
            out.append(Finding("R6", "high", rel, i, line.strip()[:120], RULE_SOURCES["R6"]))
        if prereq_zone and INSTALL_CMD.search(line):
            out.append(Finding("R5", "medium", rel, i, line.strip()[:120], RULE_SOURCES["R5"]))
    return out


def skill_files(skill_dir: Path) -> list[Path]:
    files: list[Path] = []
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file() and (p.suffix.lower() in TEXT_SUFFIX or p.name == "SKILL.md"):
            depth = len(p.relative_to(skill_dir).parts)
            if depth <= 3 and "__pycache__" not in p.parts and ".git" not in p.parts:
                files.append(p)
    return files


def scan_skill_dir(skill_dir: Path) -> dict:
    findings: list[Finding] = []
    errors: list[str] = []
    if not (skill_dir / "SKILL.md").exists():
        errors.append(f"missing SKILL.md in {skill_dir}")
    for f in skill_files(skill_dir):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"unreadable {f}: {exc}")
            continue
        findings.extend(scan_text(str(f.relative_to(skill_dir)), text))
    return {
        "skill": skill_dir.name,
        "errors": errors,
        "findings": [asdict(x) for x in findings],
        "blocked": any(x.severity in ("critical", "high") for x in findings),
    }

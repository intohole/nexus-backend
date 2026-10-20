#!/usr/bin/env python3
"""检查全工作区后端 requirements.txt/pyproject.toml 公共包版本与 nexus-backend/python-deps.json 一致。

用法:
    python3 nexus-backend/check_python_deps.py [工作区根目录]

退出码:
    0 全部一致
    1 存在版本不一致或缺失
"""
import json
import os
import re
import sys

LIB_REPOS = {"nexus-backend", "ironman"}


def load_deps(root: str) -> dict:
    path = os.path.join(root, "nexus-backend", "python-deps.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_req_line(line: str):
    line = line.strip()
    if not line or line.startswith(("#", "-", "git+", "http", "https")):
        return None
    if ";" in line:
        line = line.split(";")[0].strip()
    m = re.match(r"^([A-Za-z0-9_.\-\[\]]+)\s*(.*)$", line)
    if not m:
        return None
    name = m.group(1).lower()
    rest = m.group(2).strip().replace(" ", "")
    return name, rest


def target_version(rest: str) -> str:
    m = re.match(r"==([0-9][0-9A-Za-z+.\-]*)", rest)
    if m:
        return m.group(1)
    m = re.match(r">=([0-9][0-9A-Za-z+.\-]*)", rest)
    if m:
        return m.group(1)
    return ""


def check_file(path: str, deps: dict) -> list:
    problems = []
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    def norm(pkg: str) -> str:
        return pkg.split("[")[0].lower()

    def find_spec(pkg: str):
        for key, spec in deps.items():
            base = spec.get("base", key)
            if norm(key) == norm(pkg) or norm(base) == norm(pkg):
                return spec
        return None

    for raw in lines:
        parsed = parse_req_line(raw)
        if not parsed:
            continue
        name, rest = parsed
        spec = find_spec(name)
        if not spec:
            continue
        expected = spec["version"]
        actual = target_version(rest)
        if not actual:
            problems.append(f"  {name}: 未锁定版本({rest or '无版本'}),规范 {expected}")
        elif actual != expected:
            problems.append(f"  {name}: 声明 {actual}, 规范 {expected}")
    return problems


def find_requirements(root: str, ignore_dirs: tuple) -> list:
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
        if any(p in dirpath for p in ("/.git", "/node_modules", "/vendor", "/_shared")):
            continue
        for fn in filenames:
            if fn.startswith("requirements") and fn.endswith(".txt") and "sdk" not in dirpath:
                results.append(os.path.join(dirpath, fn))
    results.sort()
    return results


def find_pyprojects(root: str, ignore_dirs: tuple) -> list:
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs and d not in LIB_REPOS]
        if any(p in dirpath for p in ("/.git", "/node_modules", "/vendor", "/_shared")):
            continue
        if os.path.basename(dirpath) in LIB_REPOS:
            continue
        for fn in filenames:
            if fn == "pyproject.toml":
                results.append(os.path.join(dirpath, fn))
    results.sort()
    return results


def check_pyproject(path: str, deps: dict) -> list:
    try:
        import tomllib
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return []
    dep_list = (data.get("project") or {}).get("dependencies") or []
    problems = []
    for raw in dep_list:
        m = re.match(r"^([A-Za-z0-9_.\-\[\]]+)\s*(.*)$", raw.strip())
        if not m:
            continue
        name = m.group(1).lower()
        rest = m.group(2).strip().replace(" ", "")
        for key, spec in deps.items():
            base = spec.get("base", key)
            if name == key.lower() or name == base.lower():
                actual = target_version(rest)
                expected = spec["version"]
                if not actual:
                    problems.append(f"  {name}: 未锁定版本({rest or '无版本'}),规范 {expected}")
                elif actual != expected:
                    problems.append(f"  {name}: 声明 {actual}, 规范 {expected}")
                break
    return problems


def main() -> int:
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        data = load_deps(root)
    except FileNotFoundError:
        print("未找到 nexus-backend/python-deps.json,请确认工作区根目录")
        return 1
    deps = data["deps"]
    ignore_dirs = ("node_modules", "__pycache__", ".venv", ".git", "vendor", "sdk", "egg-info")

    total = 0
    bad_files = 0
    for path in find_requirements(root, ignore_dirs):
        problems = check_file(path, deps)
        if problems:
            bad_files += 1
            print(f"[不一致] {os.path.relpath(path, root)}")
            for p_ in problems:
                print(p_)
        total += 1

    py_total = 0
    for path in find_pyprojects(root, ignore_dirs):
        problems = check_pyproject(path, deps)
        if problems:
            bad_files += 1
            print(f"[不一致] {os.path.relpath(path, root)}")
            for p_ in problems:
                print(p_)
        py_total += 1

    print(f"\n扫描完成: requirements.txt {total} 个, pyproject.toml {py_total} 个,不一致文件 {bad_files} 个")
    return 1 if bad_files else 0


if __name__ == "__main__":
    sys.exit(main())
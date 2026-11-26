from __future__ import annotations
import ast, difflib, hashlib, json, os, re, subprocess, time, tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DROP = ROOT / "_dropzone"
META = ROOT / ".raccoon"
CFG = META / "config.toml"
HISTORY = META / "history" / "changes.jsonl"
STATE = META / "state" / "router_state.json"
SKIP = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".raccoon",
    "_dropzone",
    ".cache",
    "_vault",
}
SUPPORTED = {".py", ".toml", ".json", ".md", ".txt"}


def git(*args):
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr.strip())
    return p.stdout.strip()


def load_toml(path):
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def append_history(row):
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def patch_like(text):
    low = text.lower().lstrip()
    return low.startswith("diff --git") or any(
        x in low
        for x in ("tempatkan tepat", "sisipkan sebelum", "sisipkan setelah", "patch:")
    )


def packages():
    out = []
    for p in ROOT.rglob("__init__.py"):
        if not any(x in SKIP for x in p.parts):
            out.append(p.parent)
    for p in ROOT.iterdir():
        if p.is_dir() and not p.name.startswith(".") and p.name not in SKIP:
            out.append(p)
    return sorted(set(out))


def python_target(src, text, state):
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return None, 0.0, f"syntax error line {e.lineno}: {e.msg}"
    imports = set()
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            imports.add(n.module)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name.lower())
        elif isinstance(n, ast.Name):
            names.add(n.id.lower())
    learned = state.get("routes", {}).get(src.name, {})
    best = None
    for d in packages():
        rel = d.relative_to(ROOT).as_posix()
        dotted = rel.replace("/", ".")
        score = 0.0
        why = []
        if any(i == dotted or i.startswith(dotted + ".") for i in imports):
            score += 0.65
            why.append("import affinity")
        overlap = names & {p.lower() for p in d.parts}
        if overlap:
            score += min(0.25, 0.10 * len(overlap))
            why.append("symbol/package overlap")
        if src.stem.lower() in {p.lower() for p in d.parts}:
            score += 0.20
            why.append("module/package match")
        if learned.get("target") == rel and int(learned.get("approved", 0)) >= 2:
            score += min(0.20, 0.05 * int(learned["approved"]))
            why.append("validated route history")
        score = min(score, 0.99)
        if best is None or score > best[0]:
            best = (score, d, ", ".join(why) or "weak evidence")
    if best and best[0] >= 0.85:
        return best[1] / src.name, best[0], best[2]
    return (
        None,
        best[0] if best else 0.0,
        (
            "ambiguous destination: " + str(best[1].relative_to(ROOT))
            if best
            else "no package candidate"
        ),
    )


def target_for(src, text, state):
    ext = src.suffix.lower()
    if ext not in SUPPORTED:
        return None, 0.0, "unsupported extension"
    if patch_like(text):
        return None, 0.0, "patch/snippet requires unique target and anchor"
    if ext == ".py":
        return python_target(src, text, state)
    if ext == ".toml":
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            return None, 0.0, f"invalid TOML: {e}"
        if (
            "[pillar_domain]" in text
            or "[pillar_semantics]" in text
            or "domain_hints" in text
        ):
            return ROOT / "config/categories" / src.name, 0.90, "category schema"
        return ROOT / "config" / src.name, 0.86, "valid TOML config"
    if ext == ".md":
        return ROOT / "docs" / src.name, 0.86, "documentation"
    if ext == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            return None, 0.0, f"invalid JSON: {e}"
        return None, 0.0, "JSON destination/schema uncertain"
    return None, 0.0, "text merge schema unknown"


def route(cfg, state):
    if not DROP.is_dir():
        return
    threshold = float(cfg.get("auto_threshold", 0.85))
    scan = DROP.rglob("*") if cfg.get("recursive_scan", True) else DROP.iterdir()
    for src in sorted(p for p in scan if p.is_file()):
        rel = src.relative_to(DROP)
        if any(
            x in {"_review", "_conflicts"} for x in rel.parts
        ) or src.name.startswith("."):
            continue
        try:
            if src.stat().st_size > int(cfg.get("max_file_bytes", 2000000)):
                raise ValueError("file too large")
            text = src.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError) as e:
            print(f"[HOLD] {src}: {e}")
            continue
        dest, confidence, reason = target_for(src, text, state)
        if not cfg.get("auto_route", True):
            dest, confidence, reason = None, 0.0, "automatic routing disabled by config"
        if dest is None or confidence < threshold:
            bucket = DROP / "_review" / rel
        elif dest.exists():
            bucket = DROP / "_conflicts" / rel
            reason = f"target already exists: {dest.relative_to(ROOT)}"
        else:
            bucket = None
        if bucket:
            bucket.parent.mkdir(parents=True, exist_ok=True)
            if bucket.exists():
                print(f"[HOLD] quarantine collision: {src}")
                continue
            os.replace(src, bucket)
            append_history(
                {
                    "ts": int(time.time()),
                    "action": "quarantine",
                    "source": str(src.relative_to(ROOT)),
                    "target": str(bucket.relative_to(ROOT)),
                    "confidence": round(confidence, 3),
                    "reason": reason,
                    "validated": False,
                }
            )
            print(f"[REVIEW] {src.name}: {reason}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            print(f"[HOLD] target race: {dest}")
            continue
        os.replace(src, dest)
        state.setdefault("routes", {}).setdefault(src.name, {}).update(
            {
                "target": dest.parent.relative_to(ROOT).as_posix(),
                "last_seen": int(time.time()),
                "confidence": round(confidence, 3),
            }
        )
        append_history(
            {
                "ts": int(time.time()),
                "action": "route",
                "source": str(src.relative_to(ROOT)),
                "target": str(dest.relative_to(ROOT)),
                "confidence": round(confidence, 3),
                "reason": reason,
                "validated": False,
            }
        )
        print(f"[ROUTE] {src.name} -> {dest.relative_to(ROOT)}")


def validate():
    ok = True
    for p in ROOT.rglob("*.py"):
        if any(x in SKIP for x in p.parts):
            continue
        try:
            ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError) as e:
            print(f"[PY ERROR] {p.relative_to(ROOT)}: {e}")
            ok = False
    for p in ROOT.rglob("*.toml"):
        if any(x in SKIP for x in p.parts):
            continue
        try:
            with p.open("rb") as f:
                tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            print(f"[TOML ERROR] {p.relative_to(ROOT)}: {e}")
            ok = False
    return ok


def history_changes(base, valid):
    status = git("status", "--porcelain")
    for line in status.splitlines():
        name = line[3:].split(" -> ")[-1]
        if not name or name.startswith(".raccoon/history/"):
            continue
        old = subprocess.run(
            ["git", "show", f"{base}:{name}"], cwd=ROOT, capture_output=True, text=True
        )
        before = old.stdout if old.returncode == 0 else ""
        p = ROOT / name
        after = p.read_text(encoding="utf-8") if p.is_file() else ""
        diff = list(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile="before/" + name,
                tofile="after/" + name,
                lineterm="",
            )
        )
        append_history(
            {
                "ts": int(time.time()),
                "action": "change",
                "path": name,
                "base_commit": base,
                "validated": bool(valid),
                "diff": diff[:5000],
                "diff_truncated": len(diff) > 5000,
                "diff_sha256": hashlib.sha256("\n".join(diff).encode()).hexdigest(),
            }
        )


def cleanup(cfg):
    if not HISTORY.exists():
        return
    cutoff = int(time.time()) - int(cfg.get("retention_days", 30)) * 86400
    kept = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if (
                row.get("keep")
                or row.get("pinned")
                or int(row.get("ts", 0)) >= cutoff
                or not row.get("validated")
                or not row.get("base_commit")
            ):
                kept.append(row)
                continue
            check = subprocess.run(
                ["git", "cat-file", "-e", f"{row['base_commit']}^{{commit}}"],
                cwd=ROOT,
                capture_output=True,
            )
            if check.returncode:
                kept.append(row)
        except (ValueError, TypeError):
            kept.append({"ts": int(time.time()), "validated": False, "raw": line})
    tmp = HISTORY.with_suffix(".tmp")
    tmp.write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in kept),
        encoding="utf-8",
    )
    os.replace(tmp, HISTORY)


def apply_human_feedback(state):
    raw = os.environ.get("RACCOON_APPROVE_FILE", "").strip()
    if not raw:
        return
    if "=" not in raw:
        raise ValueError("approve_file format must be filename=target/directory")
    filename, target_dir = (part.strip() for part in raw.split("=", 1))
    if Path(filename).name != filename or "/" in filename or chr(92) in filename:
        raise ValueError("approve_file must be a basename, not a path")
    source = DROP / "_review" / filename
    target = (ROOT / target_dir).resolve()
    if not source.is_file() or not target.is_dir() or ROOT not in target.parents:
        raise ValueError("review file or approved target directory is invalid")
    content = source.read_text(encoding="utf-8")
    if source.suffix.lower() == ".py":
        ast.parse(content)
    dest = target / filename
    if dest.exists():
        raise FileExistsError(
            f"approved target already exists: {dest.relative_to(ROOT)}"
        )
    os.replace(source, dest)
    entry = state.setdefault("routes", {}).setdefault(filename, {})
    entry["target"] = target.relative_to(ROOT).as_posix()
    entry["approved"] = int(entry.get("approved", 0)) + 1
    entry["last_seen"] = int(time.time())
    append_history(
        {
            "ts": int(time.time()),
            "action": "human_approved_route",
            "source": str(source.relative_to(ROOT)),
            "target": str(dest.relative_to(ROOT)),
            "validated": False,
        }
    )
    print(f"[LEARNING] Human-approved route: {filename} -> {dest.relative_to(ROOT)}")


def finalize_run_history(base_commit, run_started, validated):
    if not HISTORY.exists():
        return
    rows = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            rows.append(line)
            continue
        if int(row.get("ts", 0)) >= run_started:
            row["base_commit"] = base_commit
            row["validated"] = bool(validated)
        rows.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
    tmp = HISTORY.with_suffix(".tmp")
    tmp.write_text("\\n".join(rows) + ("\\n" if rows else ""), encoding="utf-8")
    os.replace(tmp, HISTORY)


def apply_human_feedback(state):
    raw = os.environ.get("RACCOON_APPROVE_FILE", "").strip()
    if not raw:
        return
    if "=" not in raw:
        raise ValueError("approve_file format: filename=target/directory")
    filename, target_dir = (x.strip() for x in raw.split("=", 1))
    source = DROP / "_review" / filename
    target = (ROOT / target_dir).resolve()
    if not source.is_file() or not target.is_dir() or ROOT not in target.parents:
        raise ValueError("review file or approved target directory is invalid")
    if source.suffix.lower() == ".py":
        ast.parse(source.read_text(encoding="utf-8"))
    dest = target / filename
    if dest.exists():
        raise FileExistsError(f"target exists: {dest.relative_to(ROOT)}")
    os.replace(source, dest)
    entry = state.setdefault("routes", {}).setdefault(filename, {})
    entry["target"] = target.relative_to(ROOT).as_posix()
    entry["approved"] = int(entry.get("approved", 0)) + 1
    entry["last_seen"] = int(time.time())
    append_history(
        {
            "ts": int(time.time()),
            "action": "human_approved_route",
            "source": str(source.relative_to(ROOT)),
            "target": str(dest.relative_to(ROOT)),
            "validated": False,
        }
    )
    print(f"[LEARNING] approved {filename} -> {dest.relative_to(ROOT)}")


def finalize_run_history(base_commit, started, validated):
    if not HISTORY.exists():
        return
    rows = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            rows.append(line)
            continue
        if int(row.get("ts", 0)) >= started:
            row["base_commit"] = base_commit
            row["validated"] = bool(validated)
        rows.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
    tmp = HISTORY.with_suffix(".tmp")
    tmp.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    os.replace(tmp, HISTORY)


def main():
    c = load_toml(CFG)
    cfg = {**c.get("router", {}), **c.get("history", {})}
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"routes": {}}
    base = git("rev-parse", "HEAD")
    started = int(time.time())
    apply_human_feedback(state)
    route(cfg, state)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE)
    valid = validate()
    history_changes(base, valid)
    finalize_run_history(base, started, valid)
    cleanup(cfg)
    print("[RACCOON] routing, validation, history maintenance complete")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())

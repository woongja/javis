"""Stand-in for `claude` / `codex` in tests. Behaviour via JAVIS_FAKE_MODE."""
import os
import sys
import time
from pathlib import Path

mode = os.environ.get("JAVIS_FAKE_MODE", "ok")
if mode == "version" or "--version" in sys.argv:
    print(os.environ.get("JAVIS_FAKE_VERSION", "codex-cli 0.171.0"))
    sys.exit(0)
prompt = sys.stdin.read()
leaked = [k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY") if k in os.environ]
worker = os.environ.get("JAVIS_AGENT", "") + "|" + os.environ.get("JAVIS_WORKTREE", "")
Path(".fake-args.txt").write_text("\n".join([*sys.argv[1:], f"leaked={leaked}", f"worker={worker}"]), encoding="utf-8")

if mode == "quota":
    print("Error: Usage limit reached. Try again later.")
    sys.exit(1)
if mode == "slow":
    time.sleep(30)
    sys.exit(0)
if mode == "fail":
    print("boom", file=sys.stderr)
    sys.exit(3)

Path(".task").mkdir(exist_ok=True)
Path(".task/result.md").write_text("done: wrote hud/demo.txt\n", encoding="utf-8")
Path("hud").mkdir(exist_ok=True)
Path("hud/demo.txt").write_text("demo\n", encoding="utf-8")
print("OK")

"""Deterministic multi-project stress harness.

The default adapter is deliberately local and fake.  This module never calls
Codex or a network service unless a future adapter is explicitly opted in by
the CLI's live guard; the current live mode is therefore reported as blocked.
"""
import argparse
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .account_guard import AccountGuardError, model_turn
from .chatgpt_limits import update as update_limits
from .context_compaction import build_summary, inspect_jsonl, should_compact
from .context_isolation import isolate_generated_context
from .review import build_review_batch, import_review_payload
from .storage import lock, write


class FakeCodexAdapter:
    """Deterministic adapter: no executable, credentials, or model turns."""
    live = False

    def run(self, project, state, attempt, verify_fail_once=True, run_root=None):
        run_dir = (run_root or (project / ".ai-dev" / "runs")) / state["id"] / str(attempt)
        run_dir.mkdir(parents=True, exist_ok=True)
        log = run_dir / "codex.jsonl"
        log.write_text(json.dumps({"type": "thread.started", "thread_id": "fake-%s-%d" %
                                   (project.name, attempt)}) + "\n" +
                        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 64,
                                                                            "output_tokens": 8}}) + "\n",
                        encoding="utf-8")
        return {"ok": not (verify_fail_once and attempt == 1),
                "session_id": "fake-%s-%d" % (project.name, attempt),
                "adapter": "deterministic-fake"}


def _one(project, guard_dir, index, adapter):
    state = {"id": "task-%02d" % index, "status": "RUNNING", "prompt": "fake stress task %d" % index,
             "attempts": 0, "decisions": [], "verification": {"ok": False}}
    config = {"account_guard": {"enabled": True, "directory": str(guard_dir),
                                 "max_concurrent": 8,
                                 "tier_limits": {"cheap": 8, "terra": 8, "sol": 8, "expensive": 8}}}
    try:
        for attempt in (1, 2):
            with model_turn(config, "gpt-6-luna", "low") as guard:
                state["attempts"] = attempt
                state["decisions"].append({"model": "gpt-6-luna", "reasoning": "low",
                                            "attempt": attempt})
                # Exercise the same move/restore boundary used by supervisor.
                with isolate_generated_context(project) as isolated:
                    result = adapter.run(project, state, attempt,
                                         run_root=isolated.relocate(project / ".ai-dev" / "runs"))
                state["last_run"] = str(project / ".ai-dev" / "runs" / state["id"] / str(attempt) / "codex.jsonl")
                metrics = inspect_jsonl(state["last_run"])
                # A small deterministic log is below threshold; force the
                # compaction contract through its pure metadata functions.
                if attempt == 1:
                    metrics["context_window_exceeded"] = True
                compact, reason = should_compact(metrics)
                if compact:
                    state["compaction"] = {"compaction_reason": reason,
                                           "summary_bytes": len(build_summary(state).encode("utf-8"))}
                state["verification"] = {"ok": result["ok"], "command": ["fake-verify"]}
                if result["ok"]:
                    state["status"] = "COMPLETED"
                    break
                state["status"] = "RETRY"
        else:
            state["status"] = "BLOCKED"
            state["reason"] = "verification failed after retry"
        run_dir = project / ".ai-dev" / "runs" / state["id"]
        write(run_dir / "state.json", state)
        write(run_dir / "report.json", {"task_id": state["id"], "status": state["status"],
                                         "attempts": state["attempts"], "verification": state["verification"],
                                         "adapter": "deterministic-fake", "advisory_reason": "diagnosis",
                                         "kind": "diagnosis"})
        return {"project": project.name, "status": state["status"], "reason": state.get("reason", "")}
    except Exception as exc:
        return {"project": project.name, "status": "ERROR", "reason": type(exc).__name__ + ": " + str(exc)}


def run_stress(projects=4, live=False):
    if live:
        return {"mode": "live", "completed": 0, "blocked": 1, "errors": 0,
                "results": [{"status": "BLOCKED", "reason": "live adapter is disabled; no real model turns are permitted by this harness"}]}
    if type(projects) is not int or not 2 <= projects <= 32:
        raise ValueError("projects must be between 2 and 32")
    with tempfile.TemporaryDirectory(prefix="megaprog-stress-") as folder:
        root = Path(folder)
        guard_dir = root / "global-account-guard"
        paths = []
        for index in range(projects):
            project = root / ("project-%02d" % index)
            (project / ".ai-dev" / "runs").mkdir(parents=True)
            write(project / ".ai-dev" / "state.json", {"status": "IDLE", "project": project.name})
            (project / "marker.txt").write_text("project-%02d\n" % index, encoding="utf-8")
            paths.append(project)
        # Verify global and tier limits independently, without consuming a
        # model turn: the held slot must make the second acquisition block.
        limit_config = {"account_guard": {"directory": str(guard_dir), "max_concurrent": 1,
                                           "tier_limits": {"cheap": 1, "terra": 1, "sol": 1, "expensive": 1}}}
        with model_turn(limit_config, "gpt-6-luna", "low"):
            try:
                with model_turn(limit_config, "gpt-6-luna", "low"):
                    guard_probe = "unexpectedly acquired"
            except AccountGuardError as exc:
                guard_probe = exc.evidence.get("reason", "blocked")
        adapter = FakeCodexAdapter()
        with ThreadPoolExecutor(max_workers=projects) as executor:
            futures = [executor.submit(_one, path, guard_dir, i, adapter) for i, path in enumerate(paths)]
            results = [future.result() for future in as_completed(futures)]
        results.sort(key=lambda item: item["project"])
        # Exercise the real offline advisory contract once.  It writes only a
        # bounded untrusted artifact and increments the local ledger, never a
        # consumer ChatGPT or Codex quota.
        advisory = {"ledger": "not available", "imports": "none"}
        advisory_project = paths[0]
        update_limits(advisory_project, bucket="chat_pro", status="available",
                      observed_at="2026-09-16T00:00:00Z", source="manual_ui", confidence="low")
        batch = build_review_batch(advisory_project, max_items=1)
        if batch["items"]:
            item = batch["items"][0]
            imported = import_review_payload(advisory_project, {
                "contract_version": batch["contract_version"], "task_id": item["task_id"],
                "task_hash": item["task_hash"], "bundle_id": batch["bundle_id"],
                "repo_state_hash": batch["repo_state_hash"], "chatgpt_bucket": "chat_pro",
                "advisory": {"fake": True, "note": "bounded deterministic advisory"}})
            advisory = {"ledger": "updated", "imports": imported.get("status", "unknown")}
        # Cross-project contamination check: each marker and run tree belongs
        # only to its own project; shared guard files are intentionally global.
        contamination = []
        for project in paths:
            if project.joinpath("marker.txt").read_text(encoding="utf-8").strip() != project.name:
                contamination.append(project.name + ": marker")
            for path in (project / ".ai-dev" / "runs").rglob("*.json"):
                if project.name not in path.read_text(encoding="utf-8") and path.name == "state.json":
                    contamination.append(project.name + ": state")
        statuses = {key: sum(item["status"] == key for item in results) for key in ("COMPLETED", "BLOCKED", "ERROR")}
        return {"mode": "fake", "projects": projects, "completed": statuses["COMPLETED"],
                "blocked": statuses["BLOCKED"], "errors": statuses["ERROR"],
                "account_guard_probe": guard_probe, "chatgpt_advisory": advisory,
                "context_isolation": "checked", "cross_project_contamination": contamination,
                "results": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Deterministic multi-project MegaProg stress harness")
    parser.add_argument("--projects", type=int, default=4)
    parser.add_argument("--live", action="store_true", help="explicitly request guarded live mode (never default)")
    args = parser.parse_args(argv)
    result = run_stress(args.projects, args.live)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0

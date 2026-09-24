# Superset operation

Use the installed CLI and current workspace; identifiers below are variables, not historical IDs. Inspect relevant `--help` after a version change. Do not load every integration or catalog for an ordinary local worker.

1. Verify the requested model in the actual agent runtime. Superset's model whitelist and the agent CLI can disagree. In the September 2026 run, `agents create` rejected a model that the installed Claude CLI accepted. That was a launch-path mismatch, not proof that the account lacked the model.
2. If the preset cannot express the verified model, use a Superset terminal with the approved local runtime/preset. Do not silently substitute a model, bypass permissions/hooks, remove required MCP servers, or change authentication modes as a speed workaround.
3. Build shell command arguments with proper shell quoting. In Python, use `shlex.join(argv)` for the terminal's shell command; use an argv list for invoking Superset. `JSON.stringify` is not shell escaping.

```python
argv = ["claude", "--model", requested_model,
        "Read " + str(brief_path) + " and execute that bounded assignment."]
result = json.loads(subprocess.check_output([
    "superset", "terminals", "create", "--local", "--workspace", workspace_id,
    "--cwd", str(worktree), "--command", shlex.join(argv), "--json"
], text=True))
terminal_id = result["terminalId"]
```

Use the correct host selection instead of `--local` for a remote workspace. This example assumes the current Claude CLI's model option was verified and its configured permission profile is appropriate.

Read and message the recorded terminal using `superset terminals read` / `send`, with the same workspace and host selection. Inspect `.text` from read results; terminal creation or list membership alone proves neither liveness nor completion. Bound `--max-lines`, write full output privately when needed, and forward only relevant lines.

Messages may be queued until the worker's current call finishes. A successful `send` is delivery evidence, not acknowledgment that the new ownership/scope has taken effect. Require a small acknowledgment before consuming a reassigned queue. Preserve the original handle across wait timeouts. For process cleanup, separately establish PID/start/parent/workspace ownership; Superset's workspace list is not a complete inventory of local processes.

# Running work rounds as Claude Code cloud sessions

Worker rounds (widgets, routing, classifier, batch tooling) can run as Claude Code cloud
sessions instead of local terminals when the local usage window is the constraint. What
worked on September 24–25, 2026, and what did not.

## Starting a round

- `claude --cloud` needs a real TTY. Start it from a Superset terminal:
  `superset terminals create --local --workspace ID --command 'cd REPO && claude --cloud "$(cat brief.txt)"'`,
  then read the terminal once for the `session_…` id.
- The brief must be self-contained: the cloud VM has no `env.local`, no `.imx/`, no profile
  and no candidate data, so a round is mock-and-tests only. Name the branch the worker must
  push (`cloud/<wp>-round<n>`) and the report file.
- The VM gets the GitHub remote at the current branch **only when the Claude GitHub App is
  installed on the repository**. Otherwise the CLI uploads a bundle of the local checkout,
  including uncommitted changes to tracked files, and the session starts without an
  `origin` remote. Push the branch first so the session sees the intended head.

## Steering and reading a session

- Follow-ups: `claude -p "message" --cloud <session id>` works from any shell (no TTY) and
  prints `Sent to cloud session.`; the reply is not returned.
- `claude --cloud <session id>` without `-p` ("attach") is not enabled on this account.
- Session pages at `claude.ai/code/<session id>` show the transcript; an automated browser
  may be refused by the local permission classifier, so read them yourself.
- `claude --teleport <session id>` loads the conversation into a local terminal (a local
  session, with local usage) and checks out the session's branch **only if it was pushed**.

## Getting the work back

The session's git proxy pushes only to repositories in the account's GitHub connection.
Symptom: `git push` answers HTTP 403, "`owner/repo` is not in this session's authorized
repository set", while fetch works. Fix, once per account, before starting rounds:

1. install the Claude GitHub App on the repository (github.com/apps/claude → Configure →
   select the repository), or
2. run `/web-setup` in a local `claude` session, which sends the local `gh` token to the
   Claude account so sessions can push wherever that token can.

Then a follow-up `claude -p "push cloud/<branch> now" --cloud <session id>` finishes the
round. Poll `git ls-remote --heads origin` for the branch, validate it in the clean
pilot-runner worktree (merge, full gates, e2e), then `git merge` into j-workspace.

## Costs and limits

Cloud sessions share the account's rate limits with local sessions; the cloud credit pays
for the VM, not for a separate model budget. Chromium in the VM may not match the pinned
Playwright build; workers have linked the installed build under the expected revision to run
the browser suites (an environment fix, never committed).

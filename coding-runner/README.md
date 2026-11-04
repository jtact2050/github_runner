# Issue coding runner

Run from the parent folder (`github-runner`). The dispatcher processes the existing
open issue backlog and newly created issues in one repository. Each issue gets
its own Docker container, Git branch, persistent workspace and job state.

```bash
./run-issues.sh --build-only
./run-issues.sh --dry-run
./run-issues.sh --concurrency 2
```

The default repository comes from `GITHUB_URL` in `.env`, falling back to
`jtact2050/oaxontax`. The dispatcher stays in the foreground and polls every
60 seconds. Use `--repo OWNER/REPO` to select another repository. Starting the
dispatcher authorizes it to create PRs, request reviews, merge approved PRs and
close their issues according to the gates below.

Every new and resumed coding invocation is unattended: the model must make
context-based implementation decisions, record material assumptions, and continue
without asking questions or waiting for confirmation. Commands must use explicit,
non-interactive options without watch mode or login/setup wizards. The worker sets
`CI=1`, disables Git credential prompts, and uses plain-output pagers; Codex runs
with `-a never`, and its prompt pipe closes after delivery. These settings do not
make every third-party command non-interactive, so the model must stop and replace
commands that still request input. Actual missing external dependencies are recorded
as structured blockers after independent work is finished; production approval and
review gates still apply.

## Accounts and parallel execution

- Coding GitHub identity: **jtact2050** (`credentials/gh-coding/`). Both the host
  dispatcher and each worker verify this login through GitHub before working.
- Reviewing GitHub identity: **jacktian8888** (`credentials/gh-review/`). Keep the
  existing review runner online and its `codex-review.yml` installed on the
  target repository's default branch.
- Codex: the same live `credentials/codex/` login is mounted in every container.
  **Codex calls run concurrently; no whole-session lock is used.** Default coding
  concurrency is 2; the review runner is independent and is not counted in those
  two slots. A worker waiting for review still occupies a slot.
- Both coding and review default to **gpt-6-astra, high effort**. Coding can be
  customized with `--model` and `--effort`; review settings remain in its Compose file.

Login once to Codex using the existing review runner's login helper. All coding
containers immediately use the same cache. `tmp` is a separate tmpfs inside each
container; session files, SQLite state and CLI logs live in that issue's workspace
folder, outside the Git checkout. Each issue uses a separate host bind mount.
Package-manager caches use container-local `/tmp`
paths writable inside the coding sandbox. As explicitly requested, coding jobs
mount `/var/run/docker.sock` and add the Codex/controller users to its owning group.
They use host networking so the test harness can reach dynamically published
`127.0.0.1` database ports. Docker Desktop must support host networking; this host
was verified with a disposable PostgreSQL connection. Use random/free test ports.
This grants access to the host Docker daemon, including its containers and volumes;
the worker prompt limits use to temporary services for the current issue. The
review runner remains separate and does not receive this mount.

Codex manages token refresh. Version 0.165.0 reloads shared credentials before
refreshing, but does not provide a guaranteed cross-process OAuth refresh lock.
Concurrent refresh can still require a new login. If an authentication error
occurs, stop affected jobs, log in once again, and retry them. Do not maintain
independently rotating copies of the same `auth.json`, or restore stale copies.
Account rate and usage limits still apply across all containers.

To log in to GitHub for coding on a new machine after building the image:

```bash
mkdir -p credentials/gh-coding
chmod 700 credentials/gh-coding
docker run --rm -it \
  --mount "type=bind,source=$PWD/credentials/gh-coding,target=/home/runner/.config/gh" \
  --entrypoint /opt/codex-coding/gh-login.sh codex-issue-worker:local
```

Choose **jtact2050** during login. The review account needs repository write access
to submit approvals; the coding account needs permission to push branches,
request reviews, dispatch GitHub Actions workflows (Actions write permission),
merge, and close issues. GitHub credentials are never supplied
to the model process. The Python controller performs GitHub mutations with the
coding login, while Codex runs under a separate Linux user.

## Milestone ordering

Every poll queries all open milestones and open issues in the repository.
Milestone number sets priority (ascending by default), followed by issue number.
The dispatcher fills available slots with dependency-ready issues across those
milestones. It does not wait for one milestone to finish before considering the
next: an independent later issue can run while earlier work is running, stopped,
or waiting on prerequisites. Unmilestoned issues and closed milestones are excluded.

```bash
./run-issues.sh --milestones 9,10,11 --concurrency 2
```

This restricts eligible milestones and overrides their priority order. Dependencies
still apply even when the prerequisite belongs to a different or excluded milestone.
An issue is deferred until all prerequisites close as completed; selecting a prerequisite
for a worker does not make its dependents ready in the same batch. Cycles remain
deferred. Fresh GitHub milestone assignments determine priority on every poll;
old saved milestone assignments do not hold the queue.

The query follows native `blocked_by` links, including all result pages. The
inverse `blocking` list describes downstream consumers and does not prevent a
producer from running. Prerequisites closed as `not_planned` or `duplicate` still
defer their dependents: replace/remove an obsolete link or complete the required
producer. Legacy closed issues without a close reason are treated as completed.
GitHub completion must represent an available, tested output; the selector cannot
prove arbitrary manually closed issues have an implementation on the target branch.
Before launching a container, the dispatcher rereads the issue, milestone, blocked
label and dependencies. A failed lookup cannot authorize a launch. Changes after
that final check cannot atomically cancel an already starting/running worker.

There is no saved dispatcher backlog or pending-issue queue. Each poll reads
GitHub again and orders the current open issues by milestone and issue number.
The dispatcher saves running/launching ownership to avoid duplicate workers.
Individual worker state records failure details and continuation instructions.
Legacy blocked, done and queued dispatcher records are ignored on startup and
removed on the next state save.

Pickup is deferred by current GitHub `status:blocked` labels or unsatisfied native
`blocked_by` dependencies. Issue titles, historical worker failures and completion
criteria in prose do not create pickup dependencies. Stopped workers, including
unfinished/failed CLI runs and reported external blockers, are reconsidered
automatically on the next poll within the remaining round budget. Workers still
read completion criteria and must satisfy the actual acceptance/review gates before closure.

After a worker stops, its container is cleaned up and its scheduling entry is
removed. If GitHub still reports that issue open and ready, the next poll can
select it again automatically. Old manual-retry flags are ignored during selection
and removed when the saved worker resumes; no special restart option is needed.
The same issue's workspace and workflow progress are retained to resume
implementation or review. The cumulative CLI invocation limit still applies.

## One uninterrupted CLI run for implementation and test repair

Local validation follows the repository's automatic PR/push CI workflows: the
same test selection, runtime/package-manager versions, service images and setup,
including separately named required test jobs. The AI uses the repository's local
aggregate command when it covers those lanes, adds omitted CI checks, respects
local test budgets, and bounds concurrency to available resources. It does not
run both the aggregate and all its constituent lanes again. Focused checks run
during fixes; unchanged passing results can be reused across resumed sessions.

Extended, exhaustive, stress/capacity and manual-only suites require an explicit
user or actionable reviewer request for that suite/scenario. Selecting an extended
file directly is also opt-in. General implementation/acceptance requirements and
an earlier agent's plan do not opt in. Functional acceptance criteria, meaningful
critical regression coverage and known product failures still must be addressed;
tests and assertions must not be weakened to shorten a run. Optional unrun or
interrupted checks are reported as limitations in `tests`/`summary`, not claimed
as passes or made into automatic retry gates. `test_results` reports actual latest
outcomes for required CI-equivalent checks and explicitly requested extra checks.
This policy is sent on every new and resumed invocation. Rebuild the coding image
to apply prompt changes to newly launched containers; existing invocations retain
their original prompt until stopped and resumed with the rebuilt image.

An implementation attempt uses one `codex exec` process. The AI implements the
issue, runs relevant tests, diagnoses failures, fixes them, and repeats validation
inside that process until its local work is complete. The default wall-clock
limit is **7200 seconds (2 hours)**; `--cli-timeout SECONDS` changes it. This is a
maximum, not a minimum runtime or a per-command timeout. Time spent on commands,
tests, model responses and CLI retries all counts toward it. On timeout, the
controller terminates the CLI process group, allows up to 10 seconds to exit,
and then kills remaining processes in that group.

When the model returns `in_progress`, reports missing/failed tests, declares a
blocker, fails, or times out, the controller saves the workspace, conversation
and diagnostics and stops that container's run. The dispatcher automatically
reconsiders it on the next poll, using live GitHub readiness and the remaining
round budget. An ordinary dispatcher restart also resumes eligible saved work.
A genuine blocker must include its owner, evidence and unblock condition. The
prompt requires the AI to challenge blockers and finish other scoped work within
the same invocation, rather than yielding for another controller round.

PR creation still requires `completed`, no remaining steps or blocker, and
nonempty `test_results` whose reported exit codes are all zero. Independent CI
and current-head approval remain mandatory before merge. The controller integrates
`main` only when GitHub reports PR merge conflicts (`mergeable=false` or
`mergeable_state=dirty`), or to finish an already-started merge. A clean advance
of `main`, a behind branch, or unknown mergeability does not trigger integration
or another coding round. After conflict repair, the combined result must pass
validation before publishing and obtaining approval for the new head. Changed
issue requirements and publication failures preserve the handoff for the next
automatic pickup.

The container stays available through PR creation, CI, review and merge. New CI
failures or reviewer feedback can start a separate uninterrupted revision run
using `codex exec resume <SESSION_ID>`. Each revision also gets the configured
CLI timeout. The `--max-rounds` budget counts CLI invocations across automatic
retries, base integration, and review/CI revisions, not individual test/fix steps
within a process. If the budget is exhausted during integration, the merge and
continuation instructions remain saved for a later run with a higher limit.

If no review result arrives within **15 minutes of requesting review**, the worker
directly dispatches `codex-review.yml` on the repository's default branch for that
PR. It repeats at most once every 15 minutes while waiting, including after a
failed or missing GitHub workflow trigger. The dispatch cooldown is persisted
before the API call, so restarts and ambiguous network failures cannot cause a
retry storm. New review requests reset the timer. This also covers review runs
attached to the default branch that do not appear in the PR commit's checks.
The trusted review controller still requires passing CI and validates the current
PR; pending CI may cause a dispatch to exit without reviewing. Approval and merge
gates, coding round budgets, and the overall `--review-timeout` remain unchanged.

Conversation IDs are saved as soon as `thread.started` is emitted and scoped to
the repository, issue and branch. A pending event-log pointer recovers the ID
after a crash. `--last` and `--ephemeral` are never used. An unexpected ID requires
inspection instead of silently starting another conversation. Reopened issues
and replacement attempts after a PR closes start new conversations in fresh folders.
Previous results and original review/CI feedback remain available on retry.

## Live progress

Every CLI run writes these private files while it is running:

- `jobs/N/progress.log`: append-only readable history across invocations, including
  emitted messages, plan updates, commands, command exit codes and output, file
  changes, stderr excerpts and heartbeats. The same run's history also appears
  in its own `runs/<run-id>/round-*/progress.log`.
- `jobs/N/progress.json`: atomically replaced current CLI snapshot, normally
  refreshed each second. It includes PID, session ID, timestamps, elapsed and
  remaining seconds, last activity, time since output, active commands/items,
  plan, completed/failed command counts, output byte counts, emitted token usage,
  and final status/result or failure. Each invocation keeps its own snapshot too.
- `runs/<run-id>/round-*/cli-events.jsonl`: every valid emitted CLI event with
  observation timestamp, elapsed time and byte offset into the raw `stdout.log`.
- `stdout.log` and `stderr.log`: complete raw CLI streams, written directly while
  running and retained on failure. Commands may emit their output to the CLI event
  stream only on completion; the runner cannot expose output the CLI hasn't emitted.

Heartbeats default to every **30 seconds** (`--progress-interval SECONDS`). They
appear in `progress.log`, `issue-log.jsonl` as `codex_heartbeat`, and `docker logs`.
The dispatcher terminal does not stream worker logs; follow the host progress
file in another terminal. For issue #16 in the default repository:

```bash
tail -F .coding-state/238b90463879/jobs/16/progress.log
cat .coding-state/238b90463879/jobs/16/progress.json
```

A heartbeat shows observed activity, not a guarantee of correctness. A long time
without output may mean a long-running command or model response. A stale
`updated_at` can mean the worker stopped abruptly; check the container and
`worker.json` before assuming it is still running. Failed-command counts include
failures later fixed in the same invocation. Final test results determine local
readiness. CLI `completed` does not mean the PR is merged: the overall lifecycle
is recorded in `worker.json` and `issue-log.jsonl`. Logs capture emitted reasoning
summaries and tool activity, not hidden model state.

## PR lifecycle

1. Search only open PRs. Resume the issue's existing managed PR and its branch,
   including when local state was deleted. If none exists, fetch the issue and
   default branch into a dedicated workspace and use a unique
   `codex/issue-N-attempt-<id>` branch. Refuse ambiguous or unrelated open PRs.
   Closed PR history never blocks a new attempt. If a saved PR is no longer open,
   archive the old state and workspace automatically before proceeding; its round
   budget and recorded blockers do not carry over to the replacement attempt.
2. Before a coding round, integrate the latest base branch (`main` by default)
   only if the PR has merge conflicts. Finish any preserved interrupted merge
   using its actual target. Preserve unfinished edits with a local checkpoint;
   resolve conflicts together with the scoped changes and validate the result.
   Codex implements and tests the issue using the installed Node 22, pnpm 11.7.0,
   Python, Git and compiler toolchain. It runs tests, repairs failures, and continues
   within one uninterrupted CLI process until its development scope is ready.
   A confirmed external blocker or exhausted budget leaves the issue open.
3. The controller commits and pushes as jtact2050, creates a draft PR with
   `Closes #N`, marks it ready, and requests jacktian8888. Existing managed PRs
   are reused on restart.
4. On every review poll, check the PR's conflict status alongside review and CI
   results. Combine conflicts, new review feedback from jacktian8888, and failed
   CI into one resumed revision run, with available private Actions log excerpts.
   Clean upstream advances and unknown mergeability do not trigger revisions.
   Publish the validated head without integrating newer upstream commits, then
   recheck GitHub's conflict status for that head on the next poll.
5. Merge only when the latest submitted review from jacktian8888 is APPROVED
   **for the current head SHA**, no newer unhandled feedback remains, checks
   are successful/neutral/skipped, commit statuses pass, and GitHub reports
   merge state CLEAN. Approval, checks, and head are re-read just before merging.
   The merge request includes the exact head SHA. No admin bypass is requested.
   A cancelled duplicate Codex-review check is ignored only after GitHub's
   workflow metadata proves a newer successful run of that same review workflow
   for the same PR and commit. Other failed/cancelled checks remain blockers.
6. Prefer squash merge when enabled. If main advances without causing conflicts,
   keep the approved PR head and proceed through the normal merge checks. Repair
   conflicting PRs before waiting for CI, because GitHub does not run pull-request
   CI on conflicting branches. Conflict repair requires validation and a new
   review. Final approval, CI, and mergeability gates remain required: conflicts
   appearing during the final merge check are handled on the next poll. Branch
   protection remains enforced; a non-conflict restriction does not trigger a
   base merge or an admin bypass.
7. Confirm the PR is merged, then close the issue with `state_reason=completed`
   if GitHub has not already closed it. Verify closure before exiting successfully.

Configure required reviews and CI in GitHub rulesets/branch protection if they
must also constrain other actors. This controller does not change those settings.
The host Docker daemon must have the test images cached (for this repository,
`docker pull postgres:17-alpine`). Other external services still need suitable
infrastructure; the worker must report unavailable evidence, not invent a pass.

## Operations and recovery

```bash
# Continuously run up to three independent eligible issues.
./run-issues.sh --concurrency 3

# After stopping the existing dispatcher, finish its workers and exit after cleanup.
./run-issues.sh --concurrency 1 --drain

# Launch up to two jobs once; containers continue after this command exits.
./run-issues.sh --once

# After closing a PR, use the normal command. Open issues resume an open managed
# PR if one exists, or automatically start a new attempt with archived old work.
./run-issues.sh --concurrency 1 --max-rounds 20

# One CLI run gets 2 hours by default; allow 3 hours and 15-second heartbeats.
./run-issues.sh --concurrency 1 --cli-timeout 10800 --progress-interval 15

# Increase the total CLI invocation budget if it was exhausted.
./run-issues.sh --max-rounds 20

# Tune worker resources and polling.
./run-issues.sh --concurrency 3 --cpus 2 --memory 8g --poll-seconds 60

docker ps --filter label=codex.repo=jtact2050/oaxontax
docker logs -f CONTAINER_NAME
```

The default limits are 8 CLI runs per issue (`--max-rounds`), 2 hours per CLI run
(`--cli-timeout`, seconds), and 24 hours per review/CI wait (`--review-timeout`,
seconds). A run's timeout is not reset when the AI starts another command or test.
Failed jobs stop and preserve their PR, branch, workspace and private logs.
Their next selection depends on current GitHub readiness, free slots and the
remaining coding budget. A round is one uninterrupted CLI invocation; the counter
is cumulative across container restarts. Before launching a stopped coding attempt,
the dispatcher checks its current round count against `--max-rounds` and reports
`coding_budget_exhausted` instead of repeatedly launching an exhausted worker.
This report is computed each poll, not saved as a blocked-job list. Raising the
limit above the used count makes the issue eligible again. Review waits already
in progress can still finish without another coding round.
Automatic pickup neither resets the cumulative budget nor bypasses live
dependencies or labels. Increasing the concurrency setting does not restart
existing workers; lowering it stops new launches until running jobs fall below
the new limit.

Ctrl-C switches the dispatcher to drain mode: it admits no more issues, waits for
all already-started workers to finish their review/CI/merge/closure lifecycle,
saves logs, removes their stopped containers, and exits. Repeated Ctrl-C keeps
draining rather than interrupting workers. A blocked or failed worker is also
cleaned up with its diagnostics and workspace retained. To stop one job explicitly,
use `docker stop CONTAINER_NAME`. While dispatching continues, GitHub readiness,
the remaining budget, and free slots determine whether that
issue is picked up again.
Only one dispatcher per repository and folder is allowed by a local lock.

State is in `.coding-state/<repository-hash>/`: `dispatcher.json`, and
`jobs/N/worker.json`, `issue-log.jsonl`, `runs/`, `containers/`, and `container.log`.
Keep worker state and the workspace folder together to resume work. `dispatcher.json`
contains only in-flight ownership, not a backlog. A reopened issue whose previous
worker completed receives a new branch/workspace; its old attempt is archived
instead of reusing a merged PR to close it again.

Every new issue gets a different container filesystem and host workspace folder.
Retries reuse only that issue's workspace while its PR remains open (or before a
PR has been created). When its saved PR is no longer open, a replacement attempt
uses a fresh folder; prior state/logs and the workspace move into
`jobs/N/attempts/<attempt>/`. The replacement resumes another open managed PR for
the issue if available, or creates a new branch. No restart flag or folder deletion
is necessary. `--restart-closed-pr ISSUE` remains available for older scripts.
The old workspace is retained for recovery and never mounted into the new attempt.

Only login credentials are shared: `credentials/codex` is mounted at
`/home/codex/.codex-auth`, with auth-file links from `/workspace/.codex`.
The Codex home and `/workspace/.codex-runtime` are private to that issue's folder
and survive container replacement. They sit outside `/workspace/repo`, so they
are not committed. Credentials remain symlinks to the shared login; no credential
contents are copied into the workspace. Each review still gets a disposable Codex home. The pinned CLI's
credential-write behavior is verified to preserve the auth-file link.
Issue title/body changes trigger revalidation and a new reviewable commit; the
controller checks the requirements again immediately before merging.

Before starting another issue, the dispatcher saves logs and removes each stopped
coding container, whether its job succeeded, failed, or became blocked. Cleanup
failure prevents new launches and is retried on the next cycle. The active worker
and shared reviewer remain running. Host job state, PRs and workspace folders
are retained for diagnostics and same-issue resumption; every new issue gets
its own fresh container and workspace. Container removal records list retained
workspace folders (and legacy volumes for older containers).

## Workspace storage and cleanup

New coding jobs store their files together on the host:

```text
.coding-state/<repository-hash>/jobs/N/
  worker.json                 # workflow state and conversation ID
  issue-log.jsonl              # issue history
  runs/                       # prompts, CLI output, results, patches
  containers/                 # stopped-container diagnostics
  workspace/                  # bind-mounted as /workspace
    repo/                     # checkout, dependencies and test output
    .codex/                   # persistent Codex conversation and index
    .codex-runtime/
  attempts/<attempt>/         # previous attempts, including their workspace
  legacy-workspaces/<volume>/ # verified archives of old named volumes
```

Docker Desktop's `Docker.raw` is shared by its Linux VM; it is not a per-container
file. This change puts coding checkouts and persistent session files in the issue
folder. Images, container writable layers and Docker-managed test services still
use Docker's storage. Temporary CLI files remain in tmpfs and package caches in
container-local `/tmp`.

To discard an issue's local history and workspace, stop the dispatcher and its
worker first, then remove that issue's `jobs/N/` folder. This also discards its
archived attempts and legacy copies. A future job starts fresh locally; existing
GitHub branches and PRs are unaffected. Do not delete a folder for work you intend
to resume. Stop disposable test services separately if they are still running.

On macOS, Docker Desktop can temporarily reject a deleted-and-recreated issue
folder with a bind-mount `operation not permitted` or missing-source error. If the
host folders exist and no worker container was created, the dispatcher performs
a read-only parent-folder check in a disposable offline container and retries the
same launch once. It records this recovery in `issue-log.jsonl`. It does not change
folder permissions. If the check also fails, verify Docker Desktop file sharing
for the project folder before retrying.

For legacy named volumes, this offline command copies their contents into
`legacy-workspaces/<volume>/workspace.tar` without starting coding jobs:

```bash
python3 coding-runner/archive_workspaces.py --repo jtact2050/oaxontax
# Optionally restrict to one volume with --volume EXACT_VOLUME_NAME.
```

It refuses running workers, checks available disk space, compares the archive to
the read-only source, records a SHA-256 checksum and copies matching historical
logs/state. Tar preserves Linux permissions, hard links and credential symlinks
without dereferencing them. These archives are for inspection/recovery and are
not restored as active jobs. Existing legacy worker state is rejected on launch
rather than silently paired with an empty workspace.

The command retains original Docker volumes as a fallback. After inspecting a
verified archive, an operator can remove its exact original volume with
`docker volume rm EXACT_VOLUME_NAME`; no bulk prune is necessary. Folder cleanup
alone does not remove these legacy originals. Never delete `Docker.raw` to clean
one issue, because it contains other Docker data as well.

## Investigating issue attempts

Each issue has an append-only, timestamped history at
`.coding-state/<repository-hash>/jobs/N/issue-log.jsonl`. Every worker invocation
gets a unique `run_id`; events also identify the repository, issue, branch, PR,
and cumulative coding round. The history records round reasons, issue text,
review/CI feedback, state transitions, controller Git commands and their complete
output/exit codes, GitHub requests/responses/retries (without authorization
headers), publishing, failures with tracebacks, and final outcomes. Full CI log
responses retrieved by the controller are retained even when only an excerpt is
sent to the model. It remains in place across retries and explicit fresh attempts.

Each model call has a separate directory under `jobs/N/runs/<run-id>/round-*`:

- `context.json`, `prompt.txt`, and `command.json`: issue and feedback snapshots,
  retry reason, model/effort, workflow state, exact prompt, and CLI invocation.
- `stdout.log` and `stderr.log`: all output emitted by the model CLI, written
  directly to persistent files while it runs, including partial output on failure.
  New `stdout.log` files contain JSONL events, including the session ID and tool
  activity. `final-message.txt` contains the extracted final agent response.
- `result.json`: the validated model result, including a blocked result.
- `outcome.json`: completion or error, elapsed time, and traceback. Invalid JSON,
  oversized responses, timeouts, and interruptions retain their original output.
- `before/` and `after/`: Git head, status, tracked/staged patches and new-file
  patches, including binary diffs. Snapshot failures are recorded in the history.

`round_published` distinguishes a published revision from a successful model call;
`worker_finished` with phase `done` means the merge and issue closure were confirmed.
A process killed with SIGKILL or by host failure cannot write a final outcome;
its existing history and output remain, and container state records the exit code
and Docker's OOM flag when the dispatcher collects it.

Before removing a stopped container, the dispatcher saves its stdout, stderr,
exit state, container ID, and retained workspace folders/legacy volumes under a new
`jobs/N/containers/<snapshot-id>/` directory. `container.log` and
`container-removal.json` remain convenient latest copies. A pre-existing
`container.log` is preserved as `legacy-container.log` before replacement.
Explicit closed-PR restarts also collect container logs before removal.
Earlier `codex-round-*.log` files remain available from older worker versions.

For example, from the parent folder, substitute the repository hash and issue:

```bash
tail -f .coding-state/<repository-hash>/jobs/N/issue-log.jsonl
jq -c 'select(.event == "round_started" or .event == "round_failed" or .event == "retry_deferred" or .event == "worker_failed")' \
  .coding-state/<repository-hash>/jobs/N/issue-log.jsonl
```

Diagnostic files are private (mode `0600`) and are not automatically deleted or
uploaded. Controller-generated diagnostics redact the coding GitHub token and its
Git HTTP-auth encoding; environments and credential files are not logged. Raw
model CLI output and workspace patches may contain private repository content.
Only output actually emitted by the CLI is captured, not hidden model state or
output the CLI itself omits. Back up the complete issue folder when retaining an investigation or moving
machines. `worker.json` identifies the conversation and `workspace/` contains
its actual session history, SQLite index, checkout, and uncommitted changes.
Preserve symlinks when copying and stop workers before taking a backup.

After changing the runner code, rebuild the coding image and restart the dispatcher
to load the new code. Existing containers keep their original worker version:

```bash
docker build -f Dockerfile.codex-coding -t codex-issue-worker:local .
```

The whole parent folder can be copied, including private credential folders if
needed. Stop workers first. New machines need Docker and host Python 3.9+ and
GitHub CLI (`gh`), then rebuild the images. New coding workspaces are included
in the folder copy. Docker images, Docker-managed test-service data, and
review-runner registration are separate and are not transferred this way.
Legacy named workspaces must be archived separately before moving.

## Verification

```bash
python3 -m unittest discover -s coding-runner/tests -v
python3 -m unittest discover -s codex-review/tests -v
./run-issues.sh --dry-run
```

Dry run reads GitHub and local state only; it starts no containers and changes no
issues or PRs. The test suite uses fake GitHub responses to verify concurrency,
cross-milestone priorities and dependencies, current-commit approval, feedback handling, merge guards and
merge-before-issue-closure ordering.

The queue report includes the total `open_issues` fetched from GitHub, the ordered
`milestones`, and the milestone for each entry in `next_issues`.
`github_blocked_issues` and `waiting_on_dependencies` describe issues deferred
by the current GitHub responses while scanning for free worker slots.
`dependency_blockers` identifies their exact prerequisite numbers, states and
close reasons. `available_slots` explains the launch capacity. `ready_issues`
lists eligible nonrunning work in priority order; `next_issues` is limited to the
free slots. Dry run scans the entire eligible backlog even when every slot is
occupied, so it can show ready work without proposing another container.
Normal polling stops after filling the available slots to limit API requests;
`selection_scan_complete: false` explicitly marks that partial view. Existing
running workers are counted and left alone, even if new dependencies are added.
Dry run additionally lists `running_dependency_conflicts` for active workers whose
prerequisites are now unsatisfied, including workers outside the selected milestones.
Stopped jobs appear in `ready_issues` when GitHub readiness and the remaining
budget allow pickup. Failure details stay in each job's logs and worker state;
they do not create a manual-input gate. No pending-issue queue is saved for the next poll.

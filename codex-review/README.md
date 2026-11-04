# Dedicated Codex PR review runner

One Docker container listens for GitHub Actions jobs labeled `codex-review`.
The workflow reviews new PRs, new commits (`synchronize`), reopened PRs, PRs
marked ready, review requests, and edited PRs. Drafts are skipped. Use the
workflow's **Run workflow** action with a PR number to force another review.

A complete review in rounds 1–2 with no P0/P1/P2 findings submits **APPROVE**.
Starting with round 3, a complete review submits **APPROVE** when no P0/P1 findings
remain. P2 findings stay visible as nonblocking follow-ups. P0/P1 findings still
submit **REQUEST_CHANGES**. After round 3, reviews focus on unresolved P0/P1
findings, their fixes, and serious regressions in new changes, without restarting
a broad review. Findings include file/line references in the review body. This is static
code review; tests are not executed. Authentication errors, timeouts, invalid or
incomplete output, and diffs over 1 MB fail the job without posting a decision.

Rounds are counted per PR from this review account's completed, controller-marked
reviews of distinct base/head pairs, including dismissed reviews. Historical
reviews count immediately, so existing PRs keep their progress after an upgrade
or restart. Repeated triggers or forced reviews of the same pair do not advance
the count; failed/incomplete attempts and other accounts' reviews do not count.
The current review is included in the round number. Verification and stale-commit
gates still apply at every round.

## First launch

Run these commands from `/Users/jokul/github-runner` (or your copy of this folder).

1. Set `GITHUB_URL=https://github.com/OWNER/REPOSITORY` in `.env`.
2. Get a fresh repository runner registration token in **Settings → Actions →
   Runners → New self-hosted runner**, and set `REVIEW_RUNNER_TOKEN` in `.env`.
   This is a temporary runner registration token, not a personal access token.
   The existing `RUNNER_TOKEN` for the other runners is not used or changed.
3. Build and start:

   ```sh
   docker compose -f compose.codex-review.yml up -d --build
   ```

4. Log in manually inside the container:

   ```sh
   docker compose -f compose.codex-review.yml exec codex-review /opt/codex-review/login.sh
   ```

   Follow the device-login URL/code in your terminal. Enable device code login
   in your ChatGPT security/workspace settings if necessary. Authentication stays
   in `credentials/codex/` in this project across container recreations. The
   listener starts within 30 seconds after login. You can remove the registration
   token from `.env` after registration; the `registration` volume preserves it.

   To log in to GitHub CLI as **jacktian8888** and save its login in
   `credentials/gh-review/`, run:

   ```sh
   docker exec -it github-codex-review /opt/codex-review/gh-login.sh
   ```

   This is separate from the runner registration token. The review controller
   uses this saved login to fetch code and submit reviews as **jacktian8888**.
   It verifies the identity through GitHub before reviewing and refuses a wrong
   account or a PR authored by the reviewer. Workflow token environment variables
   cannot override the saved review identity.

5. Install `.github/workflows/codex-review.yml` in the target repository's default
   branch. Give **jacktian8888** write access to the repository. The workflow uses
   `permissions: {}` because the controller authenticates with the saved account.

## Operation

CLI logins are stored in separate local folders beside the Compose file:

```text
credentials/
  codex/      # Shared live Codex login only
  gh-review/  # jacktian8888 — mounted only in the review runner
  gh-coding/  # jtact2050 — mounted only in coding containers
```

The coding dispatcher (`../run-issues.sh`) mounts its separate GitHub login and
the same live `credentials/codex/` at `/home/codex/.codex-auth`, using UID 1002.
Only the auth-file link points there; `CODEX_HOME` is private runtime storage.
Coding and review CLI sessions execute concurrently. The compatibility wrapper
is still named `codex-locked.sh`, but no longer acquires a whole-session lock.
The old `.credential.lock` file is unused. Do not copy stale auth snapshots back
over the live cache. Codex reloads and refreshes credentials itself; this CLI release
does not guarantee cross-process OAuth refresh serialization. If simultaneous
refresh fails, stop jobs, log in again once, then retry blocked jobs. All workers
share the account's usage limits.

The coding container's `CODEX_HOME/tmp` is a private 64 MB tmpfs. Reviewer
execution uses a new disposable home per invocation. Generated helper links,
process locks, runtime databases, and caches are never shared with other jobs
or copied with the login folder.

These are bind mounts, so the files can be copied with the project. Stop the
runner before copying to avoid copying credentials while they are being refreshed.
Use the copied credentials as a move to another trusted host; do not run the same
Codex session concurrently on multiple machines. A new host still needs its own
GitHub runner registration token and a unique `CODEX_RUNNER_NAME` if the old
registration is retained. Runner registration and work files remain Docker volumes.

The credentials folder is excluded from Git and Docker image builds. It contains
plaintext credentials, including GitHub's file-backed token, so keep the folder
private. Directories use mode 0700 and credential files use mode 0600; startup fixes
container file ownership after copying. The original Codex Docker volume is kept
as a migration backup but is no longer mounted; it will become stale as tokens
refresh. Copy the current local folder for future moves.

```sh
docker compose -f compose.codex-review.yml logs -f --tail=50
docker compose -f compose.codex-review.yml exec --user runner codex-review \
  sudo -H -u codex env CODEX_HOME=/home/codex/.codex codex login status
docker compose -f compose.codex-review.yml stop
```

If authentication expires, run the login command again and rerun failed jobs.
Only one review runs at a time. Commit concurrency deduplicates queued triggers. Reviews
are deduplicated by both base and head SHA; explicit review requests and manual
runs bypass deduplication, but never the verification gate.

Each Codex invocation prints a private diagnostic directory such as
`/home/runner/_work/codex-review-diagnostics/review-XXXXXXXX`. This lives on the
existing persistent work volume, outside the temporary review checkout and
Codex home. `stdout.log` retains the complete JSON response (including the
explanation when `complete` is false), `stderr.log` retains CLI progress/errors,
and `outcome.json` records the commit pair, exit code, elapsed time and outcome.
Output streams are written directly to disk, including partial output on timeout.
Directories are mode `0700` and files `0600`, owned by the controller; the model
UID cannot read them. Raw output can contain private repository material and is
never printed into Actions logs or uploaded automatically. Credential files and
the controller environment are not copied into diagnostics. Logs persist until
an operator removes the relevant completed invocation directory; monitor disk
usage. `REVIEW_DIAGNOSTICS_DIR` can select another private persistent directory.

Read the exact directory shown in the failed job's log, for example:

```sh
docker exec --user runner github-codex-review \
  cat /home/runner/_work/codex-review-diagnostics/review-XXXXXXXX/stdout.log
```

An incomplete result still fails the job without submitting a decision. After
changing the controller, rebuild its image and recreate the idle reviewer to
activate it; existing containers do not load changed host Python files:

```sh
docker compose -f compose.codex-review.yml build codex-review
docker compose -f compose.codex-review.yml up -d --no-deps codex-review
```

Code review follows verification. The workflow listens for a successful `Verify`
workflow completion and resolves open PRs at that exact head SHA. Before starting
Codex, the controller requires a successful PR `Verify` run, successful latest
other workflows, completed non-review checks (success/neutral/skipped), and
passing commit statuses. Missing or pending verification does not count as a
pass. Old workflow events cannot review a newer PR commit. Checks are read again
before posting so a CI rerun/failure during review prevents approval.

Ready-for-review and explicit review-request events are retained for PRs whose CI
already finished. If verification is still pending, these lightweight invocations
exit without starting Codex; the successful Verify completion triggers the review.
If the verification workflow is renamed, update `workflows: [Verify]` and the
controller's required workflow name together. Additional verification workflows
are checked by the gate; if they finish after Verify, request review again once
they pass.

To activate this change, install the updated workflow on the repository's default
branch and deploy the updated reviewer image/controller together. Updating only
the workflow does not update `/opt/codex-review/review.py` in an existing container.

The controller re-fetches PR state immediately before posting and binds the
review to the inspected commit. GitHub does not offer an atomic compare-and-post
review operation. Enable **Dismiss stale pull request approvals when new commits
are pushed** in your branch protection/ruleset to cover a push at that boundary.
A base-only branch push is not a PR event; use a manual review if needed.
To make requested changes block merging, configure required reviews in the
repository's branch protection/ruleset. This runner never merges PRs.

Reviews use `gpt-6-astra` with `high` reasoning effort. These are explicitly passed
to every Codex invocation, including when user configuration is ignored. Change
`CODEX_MODEL` and `CODEX_REASONING_EFFORT` in `compose.codex-review.yml` to override
them. Codex CLI is pinned to `0.165.0`; override
`REVIEW_CODEX_VERSION` to upgrade, then rebuild and verify sandbox compatibility.
The GitHub runner image follows the official `latest` tag.

## Isolation and scope

- The existing three-runner Compose file is unchanged. This is a separate project.
- There is no host Docker socket, host network, or privileged container.
- The dedicated runner registers only the custom `codex-review` label, avoiding
  ordinary jobs that select `self-hosted` or the existing `mac-docker` label.
  Labels are routing, not authorization: restrict who can edit workflows. For
  organization runners, restrict the runner group to approved workflows as well.
- The trusted controller is baked into the image. `pull_request_target` uses the
  repository's trusted workflow and never checks out or executes PR code.
- The controller fetches Git objects using the saved review account. Codex sees a bare repo
  and a diff under another Linux UID, with an allowlisted environment, read-only
  sandbox, disabled web/apps/browser tools, and no GitHub write token. PR-provided
  `AGENTS.md` and `.codex/config.toml` stay Git objects and are not loaded as policy.
- Poppler's `pdfinfo` and `pdftotext` are installed for PDF reference changes.
  The reviewer pipes PDF Git objects into these tools in its existing read-only
  sandbox, retaining page breaks and text/table layout without checking files
  out or executing repository code. It must inspect content, not only hashes.
  Unreadable, image-only, or visually essential material that cannot be assessed
  still produces an incomplete review; PDF changes are not automatically approved.
- The controller validates structured JSON and alone submits the GitHub review.
  AI reviews can still miss defects or be influenced by malicious code text;
  keep normal tests and human review requirements appropriate for your repository.
- `seccomp.json` is derived from Docker/Moby's default profile, adding only
  `clone`, `unshare`, `mount`, `umount2`, `pivot_root`, `sethostname`, and `setns`
  for Codex's nested bubblewrap sandbox. No Linux capability is added. Docker's
  other syscall restrictions remain. Linux hosts may also need to permit
  unprivileged user namespaces through their AppArmor/kernel policy.

Do not use `docker compose down -v` unless you intend to erase runner registration
and work files. Local credentials remain until you delete `credentials/`.
Restrict this persistent
runner to trusted repositories and workflow definitions.

## Verification

```sh
python3 -m unittest discover -s codex-review/tests -v
docker compose -f compose.codex-review.yml config --quiet
```

Tests cover approve/request-changes decisions, repeated/new reviews, stale head
and base commits, drafts/closed PRs, invalid/incomplete output, pagination,
environment filtering, and a real Git merge-base diff with malicious instruction
files kept inside the object database.

References: [Codex non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode),
[Codex device login](https://learn.chatgpt.com/docs/auth),
[GitHub PR reviews API](https://docs.github.com/en/rest/pulls/reviews),
[GitHub workflow approval settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository),
[Moby seccomp source](https://github.com/moby/profiles/blob/main/seccomp/default.json).

## Per-job runtime isolation

Only `auth.json` and its credential lock are shared under `/home/codex/.codex-auth`.
The runner uses a local Codex home and each review creates a fresh private home
under `/home/codex/reviews`, removed on exit. Coding jobs likewise use a private
Codex home and a workspace volume unique to their issue (or explicit new attempt).
This preserves one login without reusing sessions, memories, caches, or SQLite
runtime state between issues. Credential symlink writes were verified with the
pinned CLI 0.165.0 using a disposable fake credential; no real token was printed.

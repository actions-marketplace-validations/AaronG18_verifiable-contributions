# Threat model

This document states what a verification receipt proves, what it does not
prove, and which attacks are in scope. Being explicit about the limits is the
point of the project: a maintainer should never have to guess how much trust
to place in a badge.

## Trust anchor

The receipt is produced by a CI job defined on the repository's default
branch, triggered by `pull_request` with `contents: read`, no secrets, and
`persist-credentials: false`. The maintainer controls that workflow. A
contributor controls the PR's source and test files.

Everything below follows from that split.

## What the receipt does establish

- The two commit SHAs that were compared.
- The exact command that was run, read from the policy on the base commit.
- The digests of every test file that was copied onto the base tree.
- The exit code and output digests of both runs, in the runner's environment.
- That the head's source change was **not** copied onto the base tree.
- Whether the PR touched the verifier or its policy (`UNTRUSTED`).

If `vrc check` passes, the receipt was not edited after generation and its
verdict is consistent with the recorded exit codes. This says nothing about
**who** ran it: `vrc replay` works locally, and a locally generated receipt is
only a contributor self-check, not an attestation. The receipt records
`environment.origin` (`ci` or `local`) and the Markdown report labels the two
cases differently. Only a run produced by the maintainer-controlled workflow
carries the trust described above; a pasted local receipt carries none.

## What it does not establish

- **Correctness.** A test can fail on base and pass on head while asserting the
  wrong behaviour. This tool measures whether a test discriminates between two
  trees, not whether the change is right.
- **Authorship or understanding.** It does not detect AI-written code and does
  not record anything about a contributor. That is intentional: authorship
  detection is unreliable and punishes people who did nothing wrong.
- **Provenance and licensing.** It says nothing about where generated code came
  from or whether it can be redistributed.
- **Security of the change.** Tests are executed, not sandboxed against a
  malicious test suite. See the isolation section.
- **Cryptographic non-repudiation.** The digest is a checksum, not a signature.
  Anyone who can write the artifact can recompute it. Stronger guarantees need
  signed attestations (SLSA/in-toto/Sigstore), which are not in this MVP.

## Attacks and current mitigations

| Attack | Mitigation | Residual risk |
| --- | --- | --- |
| Paste a fake "tests pass" log in the PR body | CI generates the receipt; contributor-supplied logs are ignored | None for the receipt itself |
| Weaken the command or test paths in the same PR | Policy is read from the base commit; protected paths force `UNTRUSTED` | A previously merged malicious policy is trusted by definition; that is a review failure on the maintainer side |
| Write a vacuous test that always passes | Base run must fail, otherwise `NEEDS_EVIDENCE` | A test that fails on base for an unrelated reason is handled by the next row |
| Make the base run fail for an unrelated reason (import error, missing dependency) | The base output is classified as `assertion`, `environment` or `unknown`. Only a recognised **failure** signature yields `VERIFIED`; an import/collection error, a crash (`panic:`), a passing summary (`0 failing`, `N passed`) or an unclassified failure yields `NEEDS_RERUN`. Assertion presets contain no success tokens, lines reporting zero failures are excluded, and CI enforces this structurally in `tests/test_no_false_positive.py` | Marker matching is heuristic. A test runner with unusual output needs `assertion_markers` in the policy (or the `runner` key), otherwise it fails closed to `NEEDS_RERUN`. An attacker who controls the test code can also make an assertion fail for an unrelated reason; this tool cannot tell whether the assertion checks the claimed behaviour |
| Relabel an import error as an assertion in the receipt | `vrc check` re-runs the classifier on the recorded `failure_evidence` using the marker lists stored in the receipt | A receipt is a record, not a signature. Someone with artifact write access can replace both the evidence and the digest; see the "Alter the receipt" row |
| Game the test file overlay by placing source code under a test path | Anything under `test_paths` is copied. Root-level and common source directory names (`.`, `src/`, `lib/`, `app/`, `pkg/`, `internal/`, `packages/`) are rejected by `Config.load`; glob patterns such as `**/*_test.go` select test files explicitly, and a CI test asserts the base tree's source is unchanged | Reviewers still see the copied file list. A project that deliberately puts tests and source in one directory must use a glob — a broad pattern can still carry source files, so keep it narrow |
| Exfiltrate secrets from a test | `pull_request` has no secrets; env vars whose name contains `SECRET`, `TOKEN`, `PASSWORD`, `CREDENTIAL`, `API_KEY`, `ACCESS_KEY`, `PRIVATE`, `AUTH_CONFIG`, `WEBHOOK`, `PEM`, `COOKIE`, `KUBECONFIG` (and exact names such as `GH_PAT`) are stripped; read-only token; `persist-credentials: false` | Network egress is **not** restricted. A test can still make outbound requests. Use a runner with restricted egress for untrusted contributions |
| Escape the extracted tree via `working_directory` | Only a safe relative path is accepted (no `..`, `.`, absolute paths, backslashes or empty segments); it is read from the base policy and must exist in both extracted trees | The path is resolved inside the temporary tree; a path that escapes it is rejected at policy load |
| Malicious `git archive` member (path traversal, symlink, device) | Members are validated before extraction; absolute paths, `..`, links, devices and FIFOs are rejected | Git's own archive output is trusted |
| Alter the receipt after generation | The action runs `vrc check receipt.json` before uploading or displaying anything; `vrc check` recomputes the digest and re-derives the verdict and refuses an inconsistent receipt | No signature: an attacker with artifact write access can recompute both |
| Tamper with the verifier itself | `protected_paths` covers `vrc/`, `action.yml`, `.vrc.toml` and the whole `.github/workflows/` directory (not just `receipt.yml`), so any CI change triggers `UNTRUSTED`; merging still requires the PR approval recorded on `main` | The in-repo `uses: ./` action is loaded from the **PR head**, not the default branch, so a PR can change the action code that runs. This is contained by `pull_request` (read-only token, no secrets, no persistent credentials) and by the approval gate before merge, but it is not a sandbox. Note that the owner's agent may submit that approval for the owner's own PRs; external PRs need a human review. A compromised maintainer account defeats any in-repo control |

`tests/` is intentionally **not** a protected path. This repository dogfoods the
action on its own PRs; protecting the test suite would make every one of them
`UNTRUSTED` and destroy that signal. The verifier code itself lives in `vrc/`
and remains protected.

## Audit scope and its limits

`vrc audit` is a mutation experiment, not a specification checker. A `PINNED`
point means some test's outcome changed when that hunk was reverted; it does
not mean the assertion checks the intended behaviour. An `UNPINNED` point means
no test in the recorded suite, under the recorded command and environment,
distinguishes the two values; it does not mean the code is wrong or unused.
Deciding what assertion to add requires the change's specification, which is
not present in the diff — no sound procedure can derive it from code alone.

The audit executes the project's own test command once per change point, so it
multiplies CI cost (bounded by `--max-points`); points beyond the limit are
explicitly marked unaudited and the verdict is `INCOMPLETE`, never a passing
claim. Test paths and protected paths are excluded from change points and the
exclusions are recorded in the report. A baseline that fails, or that exits 0
without running any tests, aborts the audit: a mutation result over a red or
empty suite is meaningless. `vrc audit-check` makes the report tamper-evident by
recomputing its digest and re-deriving each status from the recorded exit code
and failure classification.

## Isolation boundary

The verifier runs the repository's own test command. That is arbitrary code
execution by design. It runs with a read-only token, no secrets, no persistent
credentials, and a timeout, but **not** inside a container sandbox or with
network isolation. For projects accepting untrusted contributions, run this in
a disposable runner with restricted network egress, and never under
`pull_request_target`.

## Reporting a problem

Use private vulnerability reporting as described in [SECURITY.md](../SECURITY.md).
Reports about evidence forgery, sandbox escapes, or secret leakage are the
highest priority.

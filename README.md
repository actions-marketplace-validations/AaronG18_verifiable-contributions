# verifiable-contributions

Give an open-source maintainer one thing they currently lack: a way to check,
without reading a wall of pasted logs, that the tests attached to a pull request
actually exercise the change.

## The problem

Generating a plausible pull request now costs almost nothing. Reviewing one
still costs the same. Linux Foundation Research (20 October 2026) found that
25% of incoming PRs are estimated to be low-quality AI output, rising to 31%
in large projects; 53% of maintainers disagree that GenAI improved community
trust, and only 35% of projects have a formal policy for AI-assisted work. The
report asks projects to put the burden of testing and disclosure on
contributors and to treat verification as core infrastructure.

A PR body saying "tests pass" is a claim, not evidence. A pasted log is also a
claim: nobody can tell whether it came from this diff, this commit, or this
environment.

## What this does

On every pull request, CI controlled by the maintainer runs the attached tests
twice:

1. On the **base tree**, with only the head's test files copied over. The source
   change is deliberately absent, so the new tests must fail.
2. On the **head tree**, unchanged, where they must pass.

It writes a structured receipt recording both runs, the exact command, the
commit SHAs, digests of the test files and of both output streams, and a
verdict. A reviewer reads one short Markdown summary in the job summary and can
reproduce the run with a single command.

Verdicts:

| Verdict | Meaning |
| --- | --- |
| `VERIFIED` | The base run failed with a **recognised assertion** and the head run passed. |
| `NEEDS_EVIDENCE` | Tests also pass on base. The test does not detect the change, so it proves nothing. |
| `NEEDS_RERUN` | Head tests fail, a run timed out, or the base failed for a non-assertion reason (import/collection error, or unrecognised output). |
| `UNTRUSTED` | The PR modifies the verifier or its policy. A human must review that first. |

A non-zero exit on the base tree is not enough. A missing module, a collection
error and a genuine assertion failure all exit with code 1, but only the last
one shows that a test detected the change. The verifier classifies the base
output and records the matching evidence line in the receipt, which `vrc check`
re-classifies to catch tampering. If your test runner uses different output, set
`runner` in the policy (e.g. `jest`, `vitest`, `go-test`, `mocha`, `npm`) or
append patterns to `assertion_markers`; unknown failures fail closed to
`NEEDS_RERUN`. See [docs/runners.md](docs/runners.md) for per-runner examples.

`VERIFIED` is deliberately narrow. It does not say the change is correct,
secure, well designed, or worth maintaining. It says a specific behaviour is
covered by a test that fails without the change. A human still reviews.

### Example job summary

For a PR whose new test genuinely covers the change, the job summary the
action appends reads (abridged):

```markdown
## Verification receipt: Verified

| | Base | Head |
| --- | --- | --- |
| Commit | `a1b2c3d4e5f6` | `9f8e7d6c5b4a` |
| Exit code | `1` | `0` |
| Failure kind | `assertion` | `none` |

The base failure was classified as an assertion failure, so the tests fail
without the change and pass with it in this CI environment. This does not
prove the change is correct, only that the claimed behaviour is exercised by a
test. A human still reviews.

Base failure evidence: `assert 9 == 8`
```

A doc-only PR (no behavioural test) instead yields `NEEDS_EVIDENCE`, because
the suite also passes on the base tree — exactly the "tests don't prove the
change" case the tool is built to surface.

## Install and use

```shell
pip install -e .
vrc validate .vrc.toml --repo .          # check the policy BEFORE opening a PR
vrc replay --base <base-sha> --head <head-sha> --output receipt.json --format summary
vrc check receipt.json
vrc explain receipt.json
```

`vrc validate` is a local preflight: it loads the policy, rejects unknown keys
and non-allowlisted executables, and checks that `working_directory` and every
`test_paths` entry exist. It runs no tests and needs no CI. This matters more
than it sounds: a workflow coming from a fork does not run until a maintainer
approves it, so without this check a broken policy is discovered only after
someone else has spent time on the PR.

`vrc replay` needs a local git repository containing both commits. It uses
`git archive` to materialise two trees in a temporary directory, so your
working tree is never touched. `vrc check` recomputes the receipt digest and
re-derives the verdict from the recorded exit codes, which catches a receipt
that was edited after the fact.

Add a `.vrc.toml` to the **default branch** of the repository you want to
verify:

```toml
command = ["python", "-m", "pytest", "-q"]
test_paths = ["tests/"]
timeout_seconds = 300
protected_paths = ["vrc/", "action.yml", ".vrc.toml", ".github/workflows/", ".github/dependabot.yml", "SECURITY.md", "pyproject.toml"]
# Optional: append markers for a test runner whose failure output is not
# recognised. Built-in markers can never be removed by this list.
# assertion_markers = ["my-runner: test failed"]
```

Unknown keys are a **hard error**. `setup-command` is not a policy key: it is
an input of the action's `with:` block in the workflow. A typo or a misplaced
key used to be silently ignored, which made a control look active when it was
not. `vrc validate` reports the valid key names.

`test_paths` must point at **test files only**: everything under it is copied
from the head tree onto the base tree. Listing `src/` or `.` would copy the
source change too and defeat the check. When tests live beside source files use
a glob such as `["**/*.test.ts"]` or `["**/*_test.go"]`. See
[docs/runners.md](docs/runners.md).

### Monorepos

The command runs at the repository root by default. In a monorepo where the
package and its tests live under a subpath, set `working_directory` and keep
`test_paths` relative to the **repository root**:

```toml
command = ["python", "-m", "pytest", "tests/test_interruption.py", "-q"]
working_directory = "libs/langgraph"
test_paths = ["libs/langgraph/tests/test_interruption.py"]
timeout_seconds = 600
```

`working_directory` must be a safe relative path (no `..`, `.`, absolute
paths, backslashes or empty segments) and must exist in both extracted trees;
a missing directory is a configuration error, not a `NEEDS_RERUN`. Both the
repository root and the subpackage are placed on `PYTHONPATH`, so the extracted
tree wins over any editable install in the runner.

The policy is read from the **base commit only**. A contributor cannot weaken
the command or the test paths in the same PR that is being checked; if they
touch a protected path, the verdict is `UNTRUSTED`.

### In GitHub Actions

```yaml
- uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
  with:
    persist-credentials: false
    fetch-depth: 0
- uses: AaronG18/verifiable-contributions@v0.5.0
  with:
    base: ${{ github.event.pull_request.base.sha }}
    head: ${{ github.event.pull_request.head.sha }}
    setup-command: python -m pip install pytest
    fail-on: NEEDS_EVIDENCE,NEEDS_RERUN
```

The action uploads `receipt.json` as an artifact and appends the Markdown
report to the job summary. `fail-on` is empty by default: the check reports
rather than gates. Turn it on once the project's test suite is reliable.

Pin the action to a commit SHA in production (this repository pins all of its own third-party actions the same way). Use `pull_request`, never
`pull_request_target`, so untrusted code never sees a writable token or
secrets.

### Auditing the test suite: `vrc audit`

`vrc replay` gives one whole-tree verdict. That verdict has a blind spot: if a
PR changes five independent lines and four of them are covered by tests, the
base run still fails and the receipt says `VERIFIED` — the fifth, completely
unobserved change is hidden.

`vrc audit` closes that gap. It splits the diff into independent hunks, reverts
**one hunk at a time** on the head tree (keeping every other change), and re-runs
the same test command:

```shell
vrc audit --base <base-sha> --head <head-sha> --output audit.json --format markdown
vrc audit-check audit.json
```

| Status | Meaning |
| --- | --- |
| `PINNED` | Reverting this change point made a recognised assertion fail. At least one test observes it. |
| `UNPINNED` | The whole suite still passed with this change point removed. No test in this suite distinguishes it. |
| `INCONCLUSIVE` | The reverted run failed for a non-assertion reason (import error, timeout, unrecognised output). No claim is made. |

The baseline head run must genuinely pass **and actually run tests**: a suite
that exits 0 while collecting zero tests is rejected as unsound rather than
reported as a green baseline. The report records how many tests passed, the
exact base/head text of each hunk, exit codes, output digests, and a
`--point N` command that reproduces any single claim. `vrc audit-check`
recomputes the digest and re-derives every status from the raw observations.

**What is and is not proven.** The measurements are reproducible facts:
"with this one hunk reverted, under this command and environment, the suite
exited 0". What the code *should* do is a specification, and the specification
is not in the diff. No tool can decide the correct expected value of a missing
assertion — that is undecidable in general (Rice's theorem). The report therefore
separates **PROVEN** facts from **SUGGESTIONS**: for an unpinned point it gives
the exact location, the old and new text, and a verification procedure (add a
test that distinguishes those two values, then re-run `--point N` and require
the status to flip to `PINNED`). The acceptance check is deterministic; the
choice of assertion remains a human decision.

`PINNED` also does not mean the assertion is *correct* — a test can pin the
wrong expected value. This tool audits the sensitivity of a test suite to a
diff; a human still judges correctness, design and security.

**Cost, stated plainly.** An audit runs the full test suite once for the
baseline plus once per change point: N points means N+1 complete runs. On a
suite that takes two minutes, three points cost roughly eight minutes. This is
inherent to mutation testing, not an inefficiency we can optimise away. Use
`--max-points` (default 20) to bound it, or `--point N` to reproduce just one
claim. Points beyond the limit are reported as unaudited and the verdict
becomes `INCOMPLETE` — never a passing claim.

### Adopting it: land the wiring in its own PR

The first PR that adds `.vrc.toml` and `verify.yml` **always** reports
`UNTRUSTED`. Those files are protected paths, so that PR cannot vouch for
itself — by design, not by accident. Review that wiring as a human, merge it on
its own, and only then will later PRs produce a usable receipt.

Do not bundle the wiring with a bug fix. The resulting receipt is `UNTRUSTED`
and says nothing about the fix, while the maintainer has to approve a workflow
from a fork, read a new CI file and carry a third-party action in their CI
forever. On a project with a healthy CI matrix that is a net cost, not help.

If you are contributing to a repository you do not control, you usually should
not ask them to adopt anything at all. Run `vrc replay --format markdown`
locally and paste the result into your PR description as **self-check
evidence**. The report explicitly labels it as a self-check, not a CI
attestation, and a reviewer can reproduce it from the recorded command and
commits. Nothing needs their approval.

## What this is not

Read [docs/threat-model.md](docs/threat-model.md) before relying on a receipt.
The short version: a receipt is a structured record, not a cryptographic
signature. Its trust comes from being produced by CI the maintainer
controls — a receipt generated locally with `vrc replay` is a self-check and
carries no such trust. `VERIFIED` does not prove that a test asserts the right
thing, that generated code is licensed cleanly, or that a contributor
understands their own patch. It also says nothing about whether the change is
semantically correct; human review still decides that.

## Status

Working MVP. The verifier runs one test command from the repository root or a
declared `working_directory` (monorepos); `vrc validate` checks a policy
locally before a PR is opened, and `vrc audit` reports per-change-point test
sensitivity. It ships built-in failure signatures for
`pytest`, and supports `jest`, `vitest`, `go test`, `mocha` and `npm` via the
`runner` policy key (see [docs/runners.md](docs/runners.md)). There is no
signing, no attestation store, and no cross-repository evidence history yet.
Issues and critical feedback are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

Licensed under Apache-2.0.

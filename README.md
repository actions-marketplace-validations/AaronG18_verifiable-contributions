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

## Install and use

```shell
pip install -e .
vrc replay --base <base-sha> --head <head-sha> --output receipt.json --format summary
vrc check receipt.json
vrc explain receipt.json
```

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

`test_paths` must point at **test files only**: everything under it is copied
from the head tree onto the base tree. Listing `src/` or `.` would copy the
source change too and defeat the check. When tests live beside source files use
a glob such as `["**/*.test.ts"]` or `["**/*_test.go"]`. See
[docs/runners.md](docs/runners.md).

The policy is read from the **base commit only**. A contributor cannot weaken
the command or the test paths in the same PR that is being checked; if they
touch a protected path, the verdict is `UNTRUSTED`.

### In GitHub Actions

```yaml
- uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
  with:
    persist-credentials: false
    fetch-depth: 0
- uses: AaronG18/verifiable-contributions@v0.3.0
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

## What this is not

Read [docs/threat-model.md](docs/threat-model.md) before relying on a receipt.
The short version: a receipt is a structured record, not a cryptographic
signature. Its trust comes from being produced by CI the maintainer controls.
It cannot prove that a test asserts the right thing, that generated code is
licensed cleanly, or that a contributor understands their own patch.

## Status

Working MVP. The verifier assumes a project's tests run from a single command in
the source tree; it ships built-in failure signatures for `pytest`, and supports
`jest`, `vitest`, `go test`, `mocha` and `npm` via the `runner` policy key (see
[docs/runners.md](docs/runners.md)). There is no signing, no attestation store,
and no cross-repository evidence history yet. Issues and critical feedback are
welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

Licensed under Apache-2.0.

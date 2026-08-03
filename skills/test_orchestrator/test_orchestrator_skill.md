# Test Orchestrator Skill

Runs the repository's relevant checks in a deliberate order, investigates failures, and reports whether they are regressions, stale expectations, environment problems, flaky tests, or real defects.

## Workflow

1. **Inspect before running.** Read the repository's contributor/README/build instructions, manifests, CI workflows, and current working-tree status. Preserve unrelated uncommitted changes.
2. **Identify the project types.** Look for Python, Rust, JavaScript/TypeScript, Go, C/C++, Java, and other build/test manifests.
3. **Run focused checks first.** For changed files, identify the nearest tests and run only those tests or targets before spending time on the full suite.
4. **Run static/format checks where established.** Prefer the project's own documented commands. Do not introduce new tooling merely to make a report look complete.
5. **Run the full supported suite.** Use the commands from CI or project documentation when available. For multi-package repositories, run the workspace/project-wide checks.
6. **Investigate every failure.** For each failure, locate the assertion and implementation, inspect recent history/blame when useful, and reproduce with the smallest focused command.
7. **Classify the result.** Use one of:
   - **Real regression:** implementation violates intended behavior.
   - **Stale test/spec:** implementation and recent intentional change agree, but the test or documentation was not updated.
   - **Environment/dependency:** missing tool, platform mismatch, unavailable service, or configuration issue.
   - **Flaky/non-deterministic:** passes on repetition or depends on timing/order/external state.
   - **Unknown:** insufficient evidence; do not overstate confidence.
8. **Report compactly.** Include commands run, pass/fail counts, failure classifications, likely impact, changed files, and recommended next action. Mention warnings separately from failures.

## Common command matrix

Use only commands supported by the repository and available environment.

| Project | Focused checks | Full checks |
|---|---|---|
| Python | `pytest path/to/test.py -q`, relevant lint/type target | `pytest`, project CI command |
| Rust | `cargo test package::module::test -- --exact` | `cargo test --workspace`, `cargo clippy --workspace`, `cargo fmt --check` when configured |
| JavaScript/TypeScript | package test with file/name filter | package test, lint, typecheck, build |
| Go | `go test ./path/... -run TestName` | `go test ./...`, `go vet ./...` |
| C/C++ | target build and focused test binary | CMake build followed by `ctest --test-dir <build>` |
| Java/Kotlin | module/class test filter | project test task and build task |

## Failure investigation rules

- Never call a test failure a product defect solely because an assertion failed.
- Compare the expectation with the current implementation and the change that introduced the behavior.
- Check for stale numeric limits, renamed APIs, changed defaults, updated schemas, and intentionally changed UI behavior.
- For truncation/encoding tests, verify both the boundary behavior and the output's encoding validity.
- For external/network tests, separate service availability from application logic.
- If a command cannot run because a dependency is missing, report that as an environment limitation rather than silently skipping it.
- Avoid modifying source or tests unless explicitly asked; if asked, make the smallest behavior-preserving fix and rerun focused plus full checks.

## Output format

```text
Test summary
- Focused checks: PASS/FAIL
- Full suite: PASS/FAIL/PARTIAL
- Static/build checks: PASS/FAIL/PARTIAL

Failures
1. <test/command> — <classification>
   Evidence: <short explanation>
   Impact: <runtime/release/CI impact>
   Action: <recommended fix or follow-up>

Warnings/limitations
- <non-failing warning or unavailable environment dependency>

Files changed during remediation
- <path>
```

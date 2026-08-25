# Contributing

Use Python 3.11 or newer and make changes on a feature branch. Do not commit personal project registries, databases, logs, recordings, prompts, source excerpts, credentials, or machine-specific paths.

Behavior changes follow test-first development: add a focused failing test, verify the expected failure, implement the smallest coherent change, then run the focused and full suites.

```bash
python3 -m unittest discover -s tests -v
node --test tests/console_state.test.js
python3 scripts/validate-plugin.py .
python3 scripts/privacy-scan.py .
git diff --check
```

Pull requests should state the user outcome, evidence, privacy impact, failure recovery, and explicit non-goals. A passing test is not proof of live installation; include a redacted real-runtime check when changing hooks, MCP, service startup, or the Codex plugin boundary.

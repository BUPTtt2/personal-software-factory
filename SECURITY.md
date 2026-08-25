# Security Policy

## Supported version

Security fixes currently target the latest `0.1.x` developer-preview release.

## Report a vulnerability

Open a private GitHub security advisory in `BUPTtt2/personal-software-factory`. Do not include real credentials, complete prompts, source code, private project paths, or ledger files in a public issue.

## Trust boundary

The console binds to loopback, validates Host and same-origin writes, and serves a bounded public projection. MCP tools in 0.1.0 are read-only. Hooks, repository content, Git metadata, and model output are untrusted input. The project uses length checks, allowlisted fields, redaction, and fail-closed output checks, but no heuristic secret scanner can guarantee detection of every credential format.

The local operator is responsible for file permissions on the user-data directory and for reviewing hook and service installation previews.

# Personal Software Factory

Personal Software Factory is a local Codex plugin that answers four practical questions about the project in front of you:

- What outcome are we pursuing?
- What is the one current action?
- What did Observer, Git, and Verifier actually see?
- What still needs a human decision?

It uses Codex lifecycle hooks, a redacted SQLite ledger, a localhost console, and five read-only MCP tools. Version 0.1.0 observes and explains; it **does not modify** project code, run arbitrary commands, merge, or deploy.

## Five-minute local setup

Requirements: macOS, Python 3.11 or newer, Codex, and Git.

```bash
git clone https://github.com/BUPTtt2/personal-software-factory.git
cd personal-software-factory
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
mkdir -p "$HOME/Library/Application Support/PersonalSoftwareFactory/config"
cp config/projects.example.json "$HOME/Library/Application Support/PersonalSoftwareFactory/config/projects.json"
```

Edit the copied registry and replace the example root with an absolute local project path. Runtime data stays in the same user-data directory, outside the plugin source.

```bash
personal-software-factory init-db
personal-software-factory serve --host 127.0.0.1 --port 8765
```

In another terminal, install the hooks after reviewing the preview:

```bash
scripts/install-observer.sh --preview
scripts/install-observer.sh --apply
```

## Install in Codex

For local development, place this repository at `~/plugins/personal-software-factory`, create the personal marketplace entry with Codex Plugin Creator, then install it:

```bash
codex plugin add personal-software-factory@personal
```

Start a new Codex task and ask:

> 查看当前项目做到哪了、下一步是什么，并说明证据边界。

The plugin will identify the current registered project, read its current status, and request bounded evidence only when needed. You can also ask it to open the localhost console.

## How monitoring works

```text
Codex Hooks -> redaction -> SQLite ledger -> policy projection -> MCP tools / localhost console
                                      ^
                              Git and Verifier receipts
```

Hooks are the low-latency signal. Git and Codex App Server reconciliation repair incomplete lifecycle information. Model statements remain claims; only structured evidence and explicit user decisions can advance completion state.

## Data boundary

- Local by default; the MCP adapter makes no external network requests.
- No complete prompts, responses, source bodies, tool output, environment variables, or credentials are exposed through MCP.
- Project roots exist only in the user's external registry and are not returned by tools.
- Removing the plugin does not delete the ledger. Data removal is a separate explicit operation.

See [SECURITY.md](SECURITY.md) for the threat model and [docs/observer-operations.md](docs/observer-operations.md) for operations.

## Maturity

`0.1.0` is a developer preview for one local user. macOS service helpers are included. Linux and Windows service management, team accounts, cloud sync, autonomous repair, and pull-request delivery are not yet supported.

## Development

```bash
python3 -m unittest discover -s tests -v
node --test tests/console_state.test.js
python3 scripts/validate-plugin.py .
python3 scripts/privacy-scan.py .
```

Contributions are welcome after reading [CONTRIBUTING.md](CONTRIBUTING.md).

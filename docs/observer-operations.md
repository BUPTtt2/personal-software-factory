# Observer Operations

Personal Software Factory stores runtime state outside its installation directory. The default macOS location is `~/Library/Application Support/PersonalSoftwareFactory`; set `SOFTWARE_FACTORY_HOME` to override it.

## Register projects

Copy `config/projects.example.json` to `$SOFTWARE_FACTORY_HOME/config/projects.json` and replace the example values. Roots must be absolute. Keep this registry private.

## Initialize and inspect

```bash
personal-software-factory init-db
personal-software-factory status --json
```

The status response contains operational counts and safe error codes, not event bodies.

## Install hooks

```bash
scripts/install-observer.sh --preview
scripts/install-observer.sh --apply
scripts/observer-status.sh --json
```

Preview is read-only. Apply updates only the owned hook entries and preserves unrelated hooks. Uninstall with `scripts/uninstall-observer.sh --preview` followed by `--apply`; ledger data is preserved.

## Run the console

```bash
personal-software-factory serve --host 127.0.0.1 --port 8765
```

For a persistent macOS LaunchAgent, review and apply:

```bash
scripts/install-console-service.sh --preview
scripts/install-console-service.sh --apply
scripts/console-service-status.sh
```

The service binds only to loopback. If the selected port belongs to another process, installation stops without replacing that process.

## Reconcile and verify

```bash
personal-software-factory reconcile --stale-minutes 30
personal-software-factory verify --cwd /absolute/project/path -- python3 -m unittest
```

Verification accepts only controlled test, lint, and build executable classes. It records command class, exit code, project identity, and evidence watermarks, not full command output.

## Recovery

- `service_unavailable`: start the console service explicitly.
- `project_not_registered`: add the project to the external registry.
- `database_busy`: wait and retry; do not start a second service.
- `capture_inactive`: preview and install hooks.
- `unknown`: reconcile App Server and Git facts; do not infer completion.

Back up the entire user-data directory only while the service is stopped. Removing the plugin or service must never remove this directory automatically.

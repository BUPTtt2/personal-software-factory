---
name: software-factory
description: Use when the user asks about a local software project's current goal, progress, next action, evidence, interruption, completion state, or wants to open the Personal Software Factory console.
---

# Personal Software Factory

Use the local factory ledger as the source of current project facts. Model recollection and task narration are not verification evidence.

## Project questions

1. Call `factory_get_current_project` with the absolute current task workspace path as `cwd`. Never use the plugin installation directory as the task workspace.
2. Call `factory_get_project_status` with the returned `projectId`.
3. Call `factory_get_evidence_summary` only when the user asks for evidence, completion confidence, a gap, or the status is uncertain.
4. When both status and evidence were read, compare their `lastEventAt`. If they differ, refresh project status once; if they still differ, report the evidence as changing rather than combining snapshots.
5. Answer in at most four lines of at most 160 characters each: goal and current stage, the one server-projected current action, evidence state, and the user decision. Write `no user decision` when none exists.

If current-project detection fails, use `factory_list_projects` only to explain what can be selected or registered. Never guess a project from remembered names.

## Console

Call `factory_open_console` only when the user asks to open, inspect, or click through the control surface. A returned URL is an entry point, not evidence that the service or project is healthy.

## Boundaries

- Preserve `unknown`, `stale`, `interrupted`, and error states exactly.
- If status or evidence fails independently, retain only facts from the successful call and name the missing boundary.
- Do not turn an Agent claim into completion evidence.
- Do not mutate a project, run verification, create a branch or PR, merge, or deploy through these read-only tools.
- If the service is unavailable, return its recovery code and stop; do not start or install background services implicitly.

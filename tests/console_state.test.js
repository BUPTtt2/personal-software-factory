const test = require("node:test");
const assert = require("node:assert/strict");

const state = require("../console/state.js");

test("project switching keeps draft status and feedback isolated", () => {
  const projects = new Map([
    ["a", state.createProjectState()],
    ["b", state.createProjectState()],
  ]);
  state.openEditor(projects.get("a"), {version: 1, outcome: "A", acceptanceCriteria: ["A1"], constraints: []}, 1);
  state.updateDraft(projects.get("a"), {outcome: "A draft", criteria: "A1", constraints: ""});
  state.setEditorMessage(projects.get("a"), "error", "A feedback");
  state.openEditor(projects.get("b"), {version: 3, outcome: "B", acceptanceCriteria: ["B1"], constraints: []}, 3);
  state.updateDraft(projects.get("b"), {outcome: "B draft", criteria: "B1", constraints: ""});

  assert.equal(state.editorView(projects.get("a")).draft.outcome, "A draft");
  assert.equal(state.editorView(projects.get("a")).status, "error");
  assert.equal(state.editorView(projects.get("a")).feedback, "A feedback");
  assert.equal(state.editorView(projects.get("b")).draft.outcome, "B draft");
  assert.equal(state.editorView(projects.get("b")).feedback, "");
});

test("work request sequences advance independently per project", () => {
  const firstProject = state.createProjectState();
  const secondProject = state.createProjectState();

  assert.equal(state.beginWorkRequest(firstProject), 1);
  assert.equal(state.beginWorkRequest(firstProject), 2);
  assert.equal(state.beginWorkRequest(secondProject), 1);
});

test("current action selection trusts the server definition and intent version", () => {
  const stale = {actionId: "stale", intentVersion: 1, isCurrent: true};
  const resolved = {actionId: "resolved", intentVersion: 2, isCurrent: false};
  const current = {actionId: "current", intentVersion: 2, isCurrent: true};

  assert.equal(state.selectCurrentAction([stale, resolved, current], null, 2), current);
  assert.equal(state.selectCurrentAction([stale, resolved], null, 2), null);
});

test("out of order polling response cannot overwrite newer project work", () => {
  const project = state.createProjectState();
  const first = state.beginWorkRequest(project);
  const second = state.beginWorkRequest(project);
  assert.equal(state.applyWorkResponse(project, second, {version: 2}, []), true);
  assert.equal(state.applyWorkResponse(project, first, {version: 1}, []), false);
  assert.equal(project.intent.version, 2);
});

test("conflict blocks save until latest intent is explicitly loaded", () => {
  const project = state.createProjectState();
  state.openEditor(project, {version: 1, outcome: "Old", acceptanceCriteria: ["Old proof"], constraints: []}, 1);
  const request = state.beginWorkRequest(project);
  state.applyWorkResponse(
    project,
    request,
    {version: 2, outcome: "Latest", acceptanceCriteria: ["Latest proof"], constraints: []},
    [],
  );
  assert.equal(project.conflict, true);
  assert.equal(state.beginIntentSave(project), false);
  state.reloadLatestDraft(project, project.intent, 2);
  assert.equal(project.draft.baseVersion, 2);
  assert.equal(project.conflict, false);
  assert.equal(state.beginIntentSave(project), true);
});

test("achievement confirmation requires the server capability", () => {
  const action = {actor: "codex", intentVersion: 2};
  assert.equal(state.canConfirmAchievement({intent_version: 2, can_confirm_achievement: false}, action, true), false);
  assert.equal(state.canConfirmAchievement({intent_version: 2, can_confirm_achievement: true}, action, true), true);
  assert.equal(state.canConfirmAchievement({intent_version: 2, can_confirm_achievement: true}, action, false), false);
});

test("acceptance review is explicit and resets on a new intent version", () => {
  const project = state.createProjectState();
  const first = state.beginWorkRequest(project);
  state.applyWorkResponse(project, first, {version: 1, acceptanceCriteria: ["Proof"]}, []);

  state.beginAcceptanceReview(project);
  assert.equal(project.acceptanceReviewOpen, true);

  const second = state.beginWorkRequest(project);
  state.applyWorkResponse(project, second, {version: 2, acceptanceCriteria: ["New proof"]}, []);
  assert.equal(project.acceptanceReviewOpen, false);
});

test("intent validation identifies the exact invalid list field", () => {
  const longItem = "x".repeat(241);

  assert.equal(state.validateIntentDraft("Goal", [longItem], []).field, "criteria");
  assert.equal(state.validateIntentDraft("Goal", ["Proof"], [longItem]).field, "constraints");
});

test("save status and feedback stay in the project state", () => {
  const project = state.createProjectState();
  state.openEditor(project, {version: 1, outcome: "Goal", acceptanceCriteria: ["Proof"]}, 1);

  assert.equal(state.beginIntentSave(project), true);
  assert.deepEqual(
    state.editorView(project),
    {draft: project.draft, status: "saving", feedback: "", saveDisabled: true},
  );
  state.finishIntentSave(project, "error", "本地账本正忙");
  assert.deepEqual(
    state.editorView(project),
    {draft: project.draft, status: "error", feedback: "本地账本正忙", saveDisabled: false},
  );
});

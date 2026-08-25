(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FactoryConsoleState = api;
}(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const CONFLICT_FEEDBACK = "目标已在其他页面更新，请核对最新版本后再保存。";

  function createProjectState() {
    return {
      intent: null,
      actions: [],
      draft: null,
      editorOpen: false,
      saving: false,
      conflict: false,
      restoreDraft: false,
      triggerId: null,
      requestSequence: 0,
      error: "",
      editorStatus: "editing",
      feedback: "",
      acceptanceReviewOpen: false,
    };
  }

  function draftFromIntent(intent, fallbackVersion) {
    return {
      outcome: intent?.outcome || "",
      criteria: (intent?.acceptanceCriteria || []).join("\n"),
      constraints: (intent?.constraints || []).join("\n"),
      baseVersion: intent?.version || fallbackVersion || 0,
    };
  }

  function openEditor(projectState, intent, fallbackVersion, triggerId) {
    projectState.draft = projectState.draft || draftFromIntent(intent, fallbackVersion);
    projectState.editorOpen = true;
    projectState.conflict = false;
    projectState.restoreDraft = true;
    projectState.triggerId = triggerId || projectState.triggerId;
    projectState.editorStatus = "editing";
    projectState.feedback = "";
  }

  function updateDraft(projectState, values) {
    projectState.draft = {
      outcome: values.outcome,
      criteria: values.criteria,
      constraints: values.constraints,
      baseVersion: projectState.draft?.baseVersion || 0,
    };
  }

  function editorView(projectState) {
    return {
      draft: projectState.draft,
      status: projectState.editorStatus,
      feedback: projectState.feedback,
      saveDisabled: projectState.saving || projectState.conflict,
    };
  }

  function setEditorMessage(projectState, status, feedback) {
    projectState.editorStatus = status;
    projectState.feedback = feedback;
  }

  function validateIntentDraft(outcome, criteria, constraints) {
    if (!outcome || outcome.length > 240) {
      return {message: "项目目标需要 1 至 240 字。", field: "outcome"};
    }
    if (criteria.length < 1 || criteria.length > 7) {
      return {message: "验收标准需要 1 至 7 条。", field: "criteria"};
    }
    if (constraints.length > 7) {
      return {message: "约束最多 7 条。", field: "constraints"};
    }
    if (criteria.some((item) => item.length > 240)) {
      return {message: "每条验收标准最多 240 字。", field: "criteria"};
    }
    if (constraints.some((item) => item.length > 240)) {
      return {message: "每条约束最多 240 字。", field: "constraints"};
    }
    return null;
  }

  function beginWorkRequest(projectState) {
    projectState.requestSequence += 1;
    return projectState.requestSequence;
  }

  function applyWorkResponse(projectState, sequence, intent, actions) {
    if (sequence !== projectState.requestSequence) return false;
    if (projectState.intent?.version !== intent?.version) {
      projectState.acceptanceReviewOpen = false;
    }
    if (
      projectState.editorOpen
      && projectState.draft
      && intent
      && projectState.draft.baseVersion !== intent.version
    ) {
      projectState.conflict = true;
      projectState.editorStatus = "conflict";
      projectState.feedback = CONFLICT_FEEDBACK;
    }
    projectState.intent = intent;
    projectState.actions = actions || [];
    projectState.error = "";
    return true;
  }

  function applyWorkFailure(projectState, sequence, message) {
    if (sequence !== projectState.requestSequence) return false;
    projectState.error = message;
    return true;
  }

  function reloadLatestDraft(projectState, intent, fallbackVersion) {
    projectState.draft = draftFromIntent(intent, fallbackVersion);
    projectState.conflict = false;
    projectState.restoreDraft = true;
    projectState.editorStatus = "editing";
    projectState.feedback = "已载入最新目标，请重新核对后保存。";
  }

  function closeEditor(projectState) {
    projectState.editorOpen = false;
    projectState.draft = null;
    projectState.saving = false;
    projectState.conflict = false;
    projectState.restoreDraft = false;
    projectState.acceptanceReviewOpen = false;
    setEditorMessage(projectState, "editing", "");
  }

  function beginIntentSave(projectState) {
    if (projectState.conflict || projectState.saving) return false;
    projectState.saving = true;
    projectState.editorStatus = "saving";
    projectState.feedback = "";
    return true;
  }

  function beginAcceptanceReview(projectState) {
    projectState.acceptanceReviewOpen = true;
  }

  function markIntentConflict(projectState) {
    projectState.conflict = true;
    projectState.saving = false;
    setEditorMessage(projectState, "conflict", CONFLICT_FEEDBACK);
  }

  function finishIntentSave(projectState, status, feedback) {
    projectState.saving = false;
    setEditorMessage(projectState, status, feedback);
  }

  function canConfirmAchievement(project, action, consistent) {
    return Boolean(
      consistent
      && project?.can_confirm_achievement === true
      && action
      && (action.intentVersion || action.intent_version) === project.intent_version
    );
  }

  function selectCurrentAction(actions, overviewAction, intentVersion) {
    return (actions || []).find((action) => (
      action.isCurrent === true
      && (action.intentVersion || action.intent_version) === intentVersion
    )) || (
      overviewAction
      && overviewAction.intent_version === intentVersion
      ? overviewAction
      : null
    );
  }

  return {
    CONFLICT_FEEDBACK,
    applyWorkFailure,
    applyWorkResponse,
    beginIntentSave,
    beginAcceptanceReview,
    beginWorkRequest,
    canConfirmAchievement,
    closeEditor,
    createProjectState,
    draftFromIntent,
    editorView,
    finishIntentSave,
    markIntentConflict,
    openEditor,
    reloadLatestDraft,
    selectCurrentAction,
    setEditorMessage,
    updateDraft,
    validateIntentDraft,
  };
}));

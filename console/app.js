(() => {
  const State = globalThis.FactoryConsoleState;
  const SELECTED_PROJECT_KEY = "software-factory:selected-project";
  const projectNav = document.getElementById("project-nav");
  const projectContext = document.getElementById("project-context");
  const title = document.getElementById("judgement-title");
  const factList = document.getElementById("fact-list");
  const fullExplanation = document.getElementById("full-explanation");
  const nextAction = document.getElementById("next-action");
  const agentAction = document.getElementById("agent-action");
  const evidenceToggle = document.getElementById("evidence-toggle");
  const evidence = document.getElementById("evidence");
  const safeError = document.getElementById("safe-error");
  const connectionState = document.getElementById("connection-state");
  const factoryShell = document.querySelector(".factory-shell");
  const workspaceGrid = document.querySelector(".workspace-grid");
  const fileLaunch = document.getElementById("file-launch");
  const systemSpine = document.getElementById("system-spine");
  const agentStatus = document.getElementById("agent-status");
  const agentTimestamp = document.getElementById("agent-timestamp");
  const agentScope = document.getElementById("agent-scope");
  const stageLabel = document.getElementById("stage-label");
  const gitBrief = document.getElementById("git-brief");
  const verificationBrief = document.getElementById("verification-brief");
  const goalStatus = document.getElementById("goal-status");
  const goalOutcome = document.getElementById("goal-outcome");
  const gapTitle = document.getElementById("gap-title");
  const actionActor = document.getElementById("action-actor");
  const completionEvidence = document.getElementById("completion-evidence");
  const actionStarted = document.getElementById("action-started");
  const intentEditor = document.getElementById("intent-editor");
  const intentOutcome = document.getElementById("intent-outcome");
  const intentCriteria = document.getElementById("intent-criteria");
  const intentConstraints = document.getElementById("intent-constraints");
  const saveIntent = document.getElementById("save-intent");
  const reloadLatestIntentButton = document.getElementById("reload-latest-intent");
  const cancelIntent = document.getElementById("cancel-intent");
  const intentFeedback = document.getElementById("intent-feedback");
  const openIntentEditorButton = document.getElementById("open-intent-editor");
  const editIntentButton = document.getElementById("edit-intent");
  const confirmAchievementButton = document.getElementById("confirm-achievement");
  const acceptanceReview = document.getElementById("acceptance-review");
  const acceptanceCriteria = document.getElementById("acceptance-criteria");
  const decisionRequest = document.getElementById("decision-request");
  const decisionQuestion = document.getElementById("decision-question");
  const decisionReason = document.getElementById("decision-reason");
  const decisionOptions = document.getElementById("decision-options");
  const decisionScope = document.getElementById("decision-scope");

  let projects = [];
  let selectedProjectId = null;
  const runIdsByProject = new Map();
  const runErrorsByProject = new Map();
  const workStateByProject = new Map();
  let overviewRequestSequence = 0;
  let evidenceOpen = false;
  let overviewError = "";

  function projectWorkState(projectId) {
    if (!workStateByProject.has(projectId)) {
      workStateByProject.set(projectId, State.createProjectState());
    }
    return workStateByProject.get(projectId);
  }

  function updateText(element, value) {
    if (element.textContent !== value) {
      element.textContent = value;
    }
  }

  function setSurfaceState(state) {
    factoryShell.dataset.state = state;
    workspaceGrid.setAttribute("aria-busy", String(state === "loading"));
    connectionState.lastChild.textContent = {
      loading: "正在连接本地服务",
      ready: "本地证据已连接",
      running: "主管正在只读检查",
      error: "本地服务需要检查",
      offline: "本地服务未启动",
    }[state];
  }

  function renderFileLaunchState() {
    workspaceGrid.hidden = true;
    fileLaunch.hidden = false;
    setSurfaceState("offline");
  }

  async function requestJson(url, options) {
    const response = await fetch(url, options);
    const value = await response.json();
    if (!response.ok) {
      const error = new Error(value.error || "request_failed");
      error.code = value.error || "request_failed";
      throw error;
    }
    return value;
  }

  function selectedLines(value) {
    return value.split("\n").map((item) => item.trim()).filter(Boolean);
  }

  function updateProjectDraft() {
    const project = selectedProject();
    if (!project) return;
    const state = projectWorkState(project.project_id);
    if (!state.editorOpen || intentEditor.dataset.projectId !== project.project_id) return;
    State.updateDraft(state, {
      outcome: intentOutcome.value,
      criteria: intentCriteria.value,
      constraints: intentConstraints.value,
    });
  }

  function actionIntentVersion(action) {
    return action?.intentVersion || action?.intent_version || 0;
  }

  function currentAction(project) {
    const state = projectWorkState(project.project_id);
    return State.selectCurrentAction(state.actions, project.current_action, project.intent_version);
  }

  function isWorkViewConsistent(project, action = currentAction(project)) {
    const intent = projectWorkState(project.project_id).intent;
    if (intent && intent.version !== project.intent_version) return false;
    return !action || actionIntentVersion(action) === project.intent_version;
  }

  function goalState(project) {
    if (project.intent_status === "unknown") return "error";
    const action = currentAction(project);
    const consistent = isWorkViewConsistent(project, action);
    if (!consistent) return "synchronizing";
    if (project.intent_status === "missing") return "no-intent";
    if (project.intent_status === "achieved") return "achievement";
    if (
      project.open_decision
      || State.canConfirmAchievement(project, action, consistent)
      || action?.actor === "user"
    ) return "confirmation-needed";
    if (action?.status === "in_progress" || (project.work_summary?.active_threads || 0) > 0) return "executing";
    return "evidence-gap";
  }

  function actorLabel(value) {
    return {codex: "Codex", user: "你", external: "外部执行"}[value] || "待确认";
  }

  function evidenceLabel(value) {
    return {
      trusted_verification_current: "当前可信验证通过",
      project_activity_observed: "Observer 记录到任务收口",
      recovered_state_verified: "恢复后现场核验通过",
      user_confirms_acceptance: "你明确确认验收",
    }[value] || "待确认";
  }

  async function refreshProjectWork(projectId) {
    const state = projectWorkState(projectId);
    const requestSequence = State.beginWorkRequest(state);
    try {
      let intent = null;
      try {
        intent = await requestJson(`/api/projects/${encodeURIComponent(projectId)}/intent`);
      } catch (error) {
        if (error.code !== "intent_not_found") throw error;
      }
      const actionsValue = await requestJson(`/api/projects/${encodeURIComponent(projectId)}/actions`);
      State.applyWorkResponse(state, requestSequence, intent, actionsValue.actions || []);
    } catch (error) {
      State.applyWorkFailure(
        state,
        requestSequence,
        `目标状态读取失败：${error.code || "request_failed"}`,
      );
    }
  }

  function selectedProject() {
    return projects.find((project) => project.project_id === selectedProjectId) || projects[0];
  }

  function compareProjectActivity(left, right) {
    const score = (project) => [
      project.active_run_id || ["queued", "running"].includes(project.agent_activity?.status) ? 1 : 0,
      project.activity_status === "recent" ? 1 : 0,
      Date.parse(project.last_event_at || "") || 0,
    ];
    const leftScore = score(left);
    const rightScore = score(right);
    for (let index = 0; index < leftScore.length; index += 1) {
      if (leftScore[index] !== rightScore[index]) return rightScore[index] - leftScore[index];
    }
    return 0;
  }

  function storedProjectId(items) {
    let stored = null;
    try {
      stored = sessionStorage.getItem(SELECTED_PROJECT_KEY);
    } catch (_) {
      stored = null;
    }
    return items.some((project) => project.project_id === stored) ? stored : null;
  }

  function preferredProjectId(items) {
    const stored = storedProjectId(items);
    if (stored) return stored;
    return [...items].sort(compareProjectActivity)[0]?.project_id || null;
  }

  function renderNavigation() {
    projectNav.replaceChildren(...projects.map((project) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "project-tab";
      button.textContent = project.display_name;
      button.dataset.projectId = project.project_id;
      button.setAttribute("aria-pressed", String(project.project_id === selectedProjectId));
      button.addEventListener("click", () => {
        updateProjectDraft();
        selectedProjectId = project.project_id;
        try {
          sessionStorage.setItem(SELECTED_PROJECT_KEY, project.project_id);
        } catch (_) {
          // Selection still applies to this page when browser storage is unavailable.
        }
        render();
      });
      return button;
    }));
  }

  function renderEvidence(project) {
    evidence.replaceChildren(...(project.evidence || []).map((item) => {
      const row = document.createElement("div");
      row.className = "evidence-row";
      const label = document.createElement("strong");
      const summary = document.createElement("span");
      label.textContent = item.source;
      summary.textContent = item.summary;
      row.append(label, summary);
      return row;
    }));
    evidence.hidden = !evidenceOpen;
    evidenceToggle.setAttribute("aria-expanded", String(evidenceOpen));
    evidenceToggle.textContent = evidenceOpen ? "收起依据" : "查看依据";
  }

  function formatTimestamp(value) {
    if (!value) return "没有运行时间";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "时间记录不可用";
    return parsed.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function renderFacts(project) {
    const items = (project.evidence || []).slice(0, 3).map((item) => {
      const row = document.createElement("li");
      const label = document.createElement("strong");
      const summary = document.createElement("span");
      label.textContent = item.source;
      summary.textContent = item.summary;
      row.append(label, summary);
      return row;
    });
    factList.replaceChildren(...items);
    factList.hidden = items.length === 0;
  }

  function renderWorkSummary(project) {
    const summary = project.work_summary || {};
    updateText(stageLabel, summary.stage || "状态未知");
    const gitIdentity = [summary.branch, summary.head].filter(Boolean).join(" @ ");
    const paths = (summary.changed_paths || []).join(" · ");
    updateText(gitBrief, paths || gitIdentity || "没有可用 Git 事实");
    const verificationCopy = {
      passed: "通过",
      failed: "失败",
      unknown: "没有可信回执",
    }[summary.verification_status] || "没有可信回执";
    const freshness = summary.verification_freshness === "stale" ? " · 已过期" : "";
    updateText(verificationBrief, `${verificationCopy}${freshness}`);
  }

  function renderAgentRecord(project) {
    const activity = project.agent_activity || {status: "not_run"};
    const hasLocalRun = project.active_run_id || runIdsByProject.has(project.project_id);
    const explicitActive = activity.status === "queued" || activity.status === "running";
    const status = hasLocalRun && !explicitActive
      ? "queued"
      : activity.status;
    const statusCopy = {
      not_run: "主管 Agent 尚未运行",
      queued: "主管 Agent 等待开始",
      running: "主管 Agent 正在只读检查",
      failed: "主管 Agent 未完成",
    };
    const completedCopy = project.source === "agent"
      ? "主管 Agent 已完成"
      : "上次 Agent 结果已过期";
    updateText(agentStatus, status === "completed" ? completedCopy : statusCopy[status] || "主管 Agent 状态未知");
    updateText(agentTimestamp, formatTimestamp(activity.completed_at || activity.started_at));
    const failureCode = status === "failed" && activity.error_code ? ` · 错误码 ${activity.error_code}` : "";
    const scope = status === "not_run"
      ? "只在你点击后，只读查看 Observer 事件、Git 元数据、验证退出码"
      : "正在只读查看 Observer 事件、Git 元数据、验证退出码";
    updateText(agentScope, `${scope}${failureCode}`);
  }

  function setCurrentSpineRole(activeRole) {
    for (const step of systemSpine.querySelectorAll(".spine-step")) {
      const isCurrent = step.dataset.role === activeRole;
      step.classList.toggle("is-current", isCurrent);
      if (isCurrent) {
        step.setAttribute("aria-current", "step");
      } else {
        step.removeAttribute("aria-current");
      }
    }
  }

  function renderSystemSpine(project) {
    const activeRole = project.active_run_id || runIdsByProject.has(project.project_id)
      ? "Agent"
      : project.source === "agent" ? "Verifier" : "Observer";
    setCurrentSpineRole(activeRole);
  }

  function renderDecision(project) {
    const decision = project.open_decision;
    decisionRequest.hidden = !decision;
    if (!decision) {
      updateText(decisionQuestion, "");
      updateText(decisionReason, "");
      updateText(decisionScope, "");
      decisionOptions.replaceChildren();
      return;
    }
    updateText(decisionQuestion, decision.question);
    updateText(decisionReason, decision.reason);
    decisionOptions.replaceChildren(...(decision.options || []).map((option) => {
      const item = document.createElement("li");
      item.textContent = option;
      return item;
    }));
    updateText(
      decisionScope,
      "当前版本只读展示这个决定。请在明确授权的后续任务中处理，系统不会伪造已保存的选择。",
    );
  }

  function renderGoalSurface(project) {
    const state = projectWorkState(project.project_id);
    const surface = goalState(project);
    const action = currentAction(project);
    const workViewConsistent = isWorkViewConsistent(project, action);
    const canConfirmAchievement = State.canConfirmAchievement(project, action, workViewConsistent);
    const canMutateWork = workViewConsistent && !state.error;
    factoryShell.dataset.goalState = surface;
    updateText(
      goalOutcome,
      workViewConsistent ? project.outcome_summary || "尚未登记项目目标" : "正在核对最新目标版本",
    );
    updateText(goalStatus, {
      "no-intent": "目标尚未登记",
      executing: "目标执行中",
      "evidence-gap": "目标存在待补缺口",
      "confirmation-needed": "目标等待你确认",
      achievement: "目标已达成",
      synchronizing: "正在同步目标与任务",
      error: "目标状态无法确认",
    }[surface]);

    const heading = {
      "no-intent": "先告诉工厂要交付什么",
      executing: action?.title || "当前任务正在执行",
      "evidence-gap": action?.title || project.judgement,
      "confirmation-needed": canConfirmAchievement
        ? "可信验证已通过，请核对验收标准"
        : action?.title || project.open_decision?.question || "有一个决定需要你确认",
      achievement: "这个项目目标已经达成",
      synchronizing: "正在核对最新目标与当前任务",
      error: "暂时无法读取已保存的项目目标",
    }[surface];
    updateText(title, heading);
    updateText(gapTitle, {
      "no-intent": "缺少一个经你确认的项目结果",
      achievement: "没有未完成缺口",
      synchronizing: "目标与任务版本尚未一致",
      error: "本地账本读取失败，不能当作没有目标",
    }[surface] || action?.whyNow || action?.why_now || project.open_decision?.reason || project.reason);
    updateText(actionActor, surface === "achievement" ? "已收口" : actorLabel(workViewConsistent ? action?.actor : null));
    updateText(
      completionEvidence,
      surface === "achievement"
        ? "已记录你的验收确认"
        : evidenceLabel(workViewConsistent ? action?.completionEvidence || action?.completion_evidence : null),
    );
    updateText(actionStarted, action && workViewConsistent ? formatTimestamp(action.createdAt || action.created_at) : "待确认");
    updateText(
      fullExplanation,
      workViewConsistent ? action?.whyNow || action?.why_now || project.reason : project.reason,
    );
    updateText(nextAction, action?.title && workViewConsistent ? `唯一当前任务：${action.title}` : "");

    const showAcceptanceReview = surface === "confirmation-needed"
      && canConfirmAchievement
      && state.acceptanceReviewOpen;
    acceptanceReview.hidden = !showAcceptanceReview;
    acceptanceCriteria.replaceChildren(...(
      showAcceptanceReview ? state.intent?.acceptanceCriteria || [] : []
    ).map((criterion) => {
      const item = document.createElement("li");
      item.textContent = criterion;
      return item;
    }));

    const editorOpen = state.editorOpen;
    const editorView = State.editorView(state);
    intentEditor.hidden = !editorOpen;
    if (editorOpen && (state.restoreDraft || intentEditor.dataset.projectId !== project.project_id)) {
      intentOutcome.value = state.draft?.outcome || "";
      intentCriteria.value = state.draft?.criteria || "";
      intentConstraints.value = state.draft?.constraints || "";
      intentEditor.dataset.projectId = project.project_id;
      state.restoreDraft = false;
    }
    intentEditor.dataset.status = editorView.status;
    updateText(intentFeedback, editorView.feedback);
    reloadLatestIntentButton.hidden = !state.conflict;
    saveIntent.disabled = editorView.saveDisabled;
    saveIntent.textContent = state.saving ? "正在保存" : "保存目标";
    openIntentEditorButton.hidden = surface !== "no-intent" || editorOpen || !canMutateWork;
    editIntentButton.hidden = !["executing", "evidence-gap", "confirmation-needed"].includes(surface) || editorOpen || !canMutateWork;
    confirmAchievementButton.hidden = surface !== "confirmation-needed"
      || !canConfirmAchievement
      || editorOpen
      || !canMutateWork;
    confirmAchievementButton.textContent = state.acceptanceReviewOpen
      ? "确认全部标准已满足"
      : "核对验收标准";
    agentAction.hidden = !["executing", "evidence-gap"].includes(surface) || editorOpen || !canMutateWork || Boolean(action);
    evidenceToggle.hidden = editorOpen;
    if (surface === "confirmation-needed" || surface === "achievement" || surface === "no-intent") {
      agentAction.disabled = true;
    }
    renderDecision(project);
  }

  function render() {
    const project = selectedProject();
    renderNavigation();
    if (!project) {
      factoryShell.dataset.goalState = "empty";
      updateText(projectContext, "没有可显示的项目活动");
      updateText(goalStatus, "项目状态不可用");
      updateText(title, overviewError ? "暂时无法读取项目事实" : "没有登记项目");
      updateText(fullExplanation, overviewError ? "本地服务响应异常，请稍后重试。" : "请先检查项目登记表。");
      factList.replaceChildren();
      factList.hidden = true;
      updateText(nextAction, "");
      updateText(agentStatus, "主管 Agent 尚未运行");
      updateText(agentTimestamp, "没有运行时间");
      safeError.textContent = overviewError;
      agentAction.disabled = true;
      evidenceOpen = false;
      evidence.replaceChildren();
      evidence.hidden = true;
      evidenceToggle.setAttribute("aria-expanded", "false");
      updateText(evidenceToggle, "查看依据");
      setCurrentSpineRole(null);
      setSurfaceState("error");
      return;
    }
    const freshness = {
      recent: "最近活动",
      stale: "记录已过期",
      none: "尚无记录",
    }[project.activity_status] || "活动状态未知";
    const eventTime = project.last_event_at ? formatTimestamp(project.last_event_at) : "没有事件时间";
    updateText(projectContext, `${project.display_name} · ${freshness} · ${eventTime}`);
    renderGoalSurface(project);
    const activeRunId = project.active_run_id || runIdsByProject.get(project.project_id);
    safeError.textContent = overviewError || runErrorsByProject.get(project.project_id)
      || projectWorkState(project.project_id).error || "";
    if (activeRunId) {
      agentAction.textContent = safeError.textContent ? "Agent 状态待确认" : "Agent 正在判断";
      agentAction.disabled = true;
      setSurfaceState(safeError.textContent ? "error" : "running");
    } else {
      agentAction.textContent = project.source === "agent" ? "再次判断" : "让 Agent 判断";
      agentAction.disabled = !project.can_run_agent;
      if (safeError.textContent) {
        setSurfaceState("error");
      } else {
        setSurfaceState("ready");
      }
    }
    renderFacts(project);
    renderWorkSummary(project);
    renderAgentRecord(project);
    renderEvidence(project);
    renderSystemSpine(project);
  }

  async function refreshOverview() {
    const requestSequence = ++overviewRequestSequence;
    try {
      const value = await requestJson("/api/overview");
      if (requestSequence !== overviewRequestSequence) {
        return;
      }
      overviewError = "";
      projects = value.projects;
      const storedProject = storedProjectId(projects);
      if (storedProject) {
        selectedProjectId = storedProject;
      } else if (!projects.some((project) => project.project_id === selectedProjectId)) {
        selectedProjectId = preferredProjectId(projects);
      }
      for (const project of projects) {
        if (project.active_run_id) {
          runIdsByProject.set(project.project_id, project.active_run_id);
        } else if (runIdsByProject.has(project.project_id)) {
          await refreshRun(project.project_id, runIdsByProject.get(project.project_id));
        }
      }
      if (selectedProjectId) {
        await refreshProjectWork(selectedProjectId);
      }
      if (requestSequence !== overviewRequestSequence) {
        return;
      }
      render();
    } catch (error) {
      if (requestSequence === overviewRequestSequence) {
        overviewError = `状态读取失败：${error.code || "request_failed"}`;
        render();
      }
    }
  }

  async function refreshRun(projectId, runId) {
    try {
      const run = await requestJson(`/api/agent-runs/${encodeURIComponent(runId)}`);
      runErrorsByProject.delete(projectId);
      if (run.status === "failed") {
        runErrorsByProject.set(projectId, `Agent 未完成：${run.error_code || "agent_failed"}`);
        if (runIdsByProject.get(projectId) === runId) runIdsByProject.delete(projectId);
      } else if (run.status === "completed") {
        runErrorsByProject.delete(projectId);
        if (runIdsByProject.get(projectId) === runId) runIdsByProject.delete(projectId);
      }
    } catch (error) {
      runErrorsByProject.set(projectId, `Agent 状态读取失败：${error.code || "request_failed"}`);
    }
  }

  async function createAgentRun() {
    const project = selectedProject();
    if (!project || !project.can_run_agent || project.active_run_id || runIdsByProject.has(project.project_id)) {
      return;
    }
    if (!isWorkViewConsistent(project)) return;
    if (currentAction(project)) return;
    safeError.textContent = "";
    runErrorsByProject.delete(project.project_id);
    const pendingKey = `pending:${project.project_id}`;
    runIdsByProject.set(project.project_id, pendingKey);
    agentAction.disabled = true;
    agentAction.textContent = "Agent 正在判断";
    setSurfaceState("running");
    renderSystemSpine(project);
    const idempotencyKey = `${project.project_id}-${Date.now()}`;
    try {
      const run = await requestJson(`/api/projects/${encodeURIComponent(project.project_id)}/agent-runs`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({idempotencyKey}),
      });
      if (runIdsByProject.get(project.project_id) === pendingKey) {
        runIdsByProject.set(project.project_id, run.run_id);
      }
      render();
    } catch (error) {
      if (project.project_id === selectedProjectId) {
        safeError.textContent = `Agent 未启动：${error.code || "request_failed"}`;
      }
      if (runIdsByProject.get(project.project_id) === pendingKey) {
        runIdsByProject.delete(project.project_id);
      }
      runErrorsByProject.set(project.project_id, `Agent 未启动：${error.code || "request_failed"}`);
      render();
    }
  }

  async function openIntentEditor(event) {
    const project = selectedProject();
    if (!project) return;
    const state = projectWorkState(project.project_id);
    if (project.intent_status !== "missing" && !state.intent) {
      await refreshProjectWork(project.project_id);
    }
    if (state.error || !isWorkViewConsistent(project)) return;
    State.openEditor(
      state,
      state.intent,
      project.intent_version,
      event?.currentTarget?.id,
    );
    render();
    intentOutcome.focus();
  }

  function closeIntentEditor() {
    const project = selectedProject();
    if (!project) return;
    const state = projectWorkState(project.project_id);
    const triggerId = state.triggerId;
    State.closeEditor(state);
    render();
    document.getElementById(triggerId)?.focus();
  }

  function reloadLatestIntent() {
    const project = selectedProject();
    if (!project) return;
    const state = projectWorkState(project.project_id);
    State.reloadLatestDraft(state, state.intent, project.intent_version);
    render();
    intentOutcome.focus();
  }

  async function submitIntent(event) {
    event.preventDefault();
    const project = selectedProject();
    if (!project) return;
    const state = projectWorkState(project.project_id);
    updateProjectDraft();
    const draft = state.draft || State.draftFromIntent(state.intent, project.intent_version);
    const outcome = draft.outcome.trim();
    const acceptanceCriteria = selectedLines(draft.criteria);
    const constraints = selectedLines(draft.constraints);
    for (const field of [intentOutcome, intentCriteria, intentConstraints]) {
      field.removeAttribute("aria-invalid");
    }
    const validationError = State.validateIntentDraft(outcome, acceptanceCriteria, constraints);
    if (validationError) {
      const invalidField = {
        outcome: intentOutcome,
        criteria: intentCriteria,
        constraints: intentConstraints,
      }[validationError.field];
      State.setEditorMessage(state, "error", validationError.message);
      invalidField.setAttribute("aria-invalid", "true");
      render();
      invalidField.focus();
      return;
    }
    if (!State.beginIntentSave(state)) return;
    render();
    try {
      const intent = await requestJson(`/api/projects/${encodeURIComponent(project.project_id)}/intent`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          version: draft.baseVersion,
          outcome,
          acceptanceCriteria,
          constraints,
        }),
      });
      state.intent = intent;
      state.error = "";
      project.intent_status = "active";
      project.intent_version = intent.version;
      project.outcome_summary = intent.outcome;
      State.closeEditor(state);
      await refreshOverview();
    } catch (error) {
      if (error.code === "intent_version_conflict") {
        State.markIntentConflict(state);
        await refreshProjectWork(project.project_id);
      } else if (error.code === "database_busy") {
        State.finishIntentSave(state, "error", "本地账本正忙，编辑内容已保留，请稍后重试。");
      } else {
        State.finishIntentSave(state, "error", `目标未保存：${error.code || "request_failed"}`);
      }
    } finally {
      render();
    }
  }

  async function confirmAchievement() {
    const project = selectedProject();
    const action = project ? currentAction(project) : null;
    const consistent = project && action && isWorkViewConsistent(project, action);
    if (!project || !State.canConfirmAchievement(project, action, consistent)) return;
    const state = projectWorkState(project.project_id);
    if (!state.acceptanceReviewOpen) {
      State.beginAcceptanceReview(state);
      render();
      acceptanceReview.focus();
      return;
    }
    confirmAchievementButton.disabled = true;
    confirmAchievementButton.textContent = "正在确认";
    safeError.textContent = "";
    try {
      await requestJson(
        `/api/projects/${encodeURIComponent(project.project_id)}/actions/${encodeURIComponent(action.actionId || action.action_id)}/decision`,
        {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({intentVersion: project.intent_version}),
        },
      );
      project.intent_status = "achieved";
      project.current_action = null;
      projectWorkState(project.project_id).actions = [];
      updateText(title, "这个项目目标已经达成");
      await refreshOverview();
    } catch (error) {
      if (["intent_version_conflict", "action_not_current", "evidence_advanced"].includes(error.code)) {
        safeError.textContent = error.code === "evidence_advanced"
          ? "确认前出现了新项目事实，请先补齐最新验证。"
          : "目标或当前任务已更新，已重新读取最新状态。";
        await refreshOverview();
      } else {
        safeError.textContent = `目标未确认：${error.code || "request_failed"}`;
      }
    } finally {
      confirmAchievementButton.disabled = false;
      render();
    }
  }

  evidenceToggle.addEventListener("click", () => {
    evidenceOpen = !evidenceOpen;
    render();
  });
  agentAction.addEventListener("click", createAgentRun);
  openIntentEditorButton.addEventListener("click", openIntentEditor);
  editIntentButton.addEventListener("click", openIntentEditor);
  cancelIntent.addEventListener("click", closeIntentEditor);
  reloadLatestIntentButton.addEventListener("click", reloadLatestIntent);
  intentOutcome.addEventListener("input", updateProjectDraft);
  intentCriteria.addEventListener("input", updateProjectDraft);
  intentConstraints.addEventListener("input", updateProjectDraft);
  intentEditor.addEventListener("submit", submitIntent);
  confirmAchievementButton.addEventListener("click", confirmAchievement);

  if (location.protocol === "file:") {
    renderFileLaunchState();
    return;
  }

  setSurfaceState("loading");
  refreshOverview();
  setInterval(refreshOverview, 3000);
})();

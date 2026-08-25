import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ConsoleAssetTests(unittest.TestCase):
    def test_html_has_minimal_agent_hierarchy(self):
        html = (ROOT / "console/index.html").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"<h1\b", html)), 1)
        self.assertEqual(len(re.findall(r'class="primary-action"', html)), 1)
        self.assertIn('aria-label="项目"', html)
        self.assertIn('id="evidence-toggle"', html)
        self.assertIn('aria-controls="evidence"', html)
        self.assertNotIn('<article class="judgement" aria-live=', html)
        self.assertIn('<h1 id="judgement-title" aria-live="polite" aria-atomic="true">', html)
        for role in ("Observer", "Agent", "Codex", "Verifier"):
            self.assertIn(role, html)

    def test_assets_are_relative_and_file_mode_is_truthful(self):
        html = (ROOT / "console/index.html").read_text(encoding="utf-8")
        script = (ROOT / "console/app.js").read_text(encoding="utf-8")
        self.assertIn('href="./app.css"', html)
        self.assertIn('src="./app.js"', html)
        self.assertIn('location.protocol === "file:"', script)
        self.assertIn("renderFileLaunchState", script)
        self.assertIn("http://127.0.0.1:8765", html)
        self.assertIn("personal-software-factory serve", html)
        self.assertNotIn(".worktrees/agent-console", html)

    def test_html_has_editorial_control_surface(self):
        html = (ROOT / "console/index.html").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"<h1\b", html)), 1)
        self.assertIn('id="connection-state"', html)
        self.assertIn('class="workspace-grid"', html)
        self.assertIn('id="system-spine"', html)
        self.assertEqual(len(re.findall(r'class="primary-action"', html)), 1)

    def test_html_has_one_screen_brief_and_agent_record(self):
        html = (ROOT / "console/index.html").read_text(encoding="utf-8")
        for anchor in (
            "project-context", "fact-list", "full-explanation", "agent-record",
            "agent-status", "agent-timestamp", "agent-scope",
        ):
            self.assertIn(f'id="{anchor}"', html)
        self.assertIn("<details", html)
        self.assertIn("完整说明", html)
        self.assertIn('id="stage-label"', html)
        self.assertIn('id="git-brief"', html)
        self.assertIn('id="verification-brief"', html)

    def test_css_uses_approved_editorial_tokens(self):
        css = (ROOT / "console/app.css").read_text(encoding="utf-8")
        for token in ("#f3f3f0", "#fcfcfa", "#181916", "#6d7068", "#d9dbd4", "#265dff"):
            self.assertIn(token, css.lower())
        for forbidden in ("gradient", "box-shadow", "backdrop-filter", "#f2aa18"):
            self.assertNotIn(forbidden, css.lower())
        self.assertIn("grid-template-columns: minmax(0, 1.65fr) minmax(280px, .75fr)", css)
        self.assertIn("@media (max-width: 820px)", css)
        self.assertIn("prefers-reduced-motion", css)

    def test_css_has_component_states_without_overlay_ui(self):
        css = (ROOT / "console/app.css").read_text(encoding="utf-8")
        self.assertIn(".system-spine", css)
        self.assertIn(".spine-step.is-current", css)
        self.assertIn('.factory-shell[data-state="loading"]', css)
        self.assertNotIn("position: fixed", css)

    def test_desktop_contract_keeps_primary_loop_in_one_view(self):
        css = (ROOT / "console/app.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-rows: 72px minmax(0, 1fr) 48px", css)
        self.assertIn("height: 100dvh", css)
        self.assertIn("-webkit-line-clamp: 3", css)
        self.assertIn(".fact-list", css)
        self.assertIn(".agent-record", css)

    def test_javascript_supports_live_product_actions(self):
        script = (ROOT / "console/app.js").read_text(encoding="utf-8")
        self.assertIn("/api/overview", script)
        self.assertIn("agent-runs", script)
        self.assertIn("setInterval", script)
        self.assertIn("runIdsByProject", script)
        self.assertIn("overviewRequestSequence", script)
        self.assertIn("runErrorsByProject", script)
        self.assertIn("let overviewError", script)
        self.assertIn("overviewError || runErrorsByProject.get", script)
        self.assertIn("pendingKey", script)
        self.assertNotIn("let activeRunId", script)
        self.assertIn("evidence-toggle", script)
        self.assertIn("selectedProjectId", script)
        self.assertIn("error_code", script)
        self.assertIn("runErrorsByProject.set(projectId, `Agent 状态读取失败", script)
        self.assertNotIn('agentAction.textContent = project.next_action', script)
        html = (ROOT / "console/index.html").read_text(encoding="utf-8")
        self.assertIn('id="next-action"', html)

    def test_goal_first_surface_has_bounded_editor_and_recovery_states(self):
        html = (ROOT / "console/index.html").read_text(encoding="utf-8")
        script = (ROOT / "console/app.js").read_text(encoding="utf-8")
        state_script = (ROOT / "console/state.js").read_text(encoding="utf-8")
        for label in ("项目目标", "当前缺口", "执行者", "完成证据"):
            self.assertIn(label, html)
        for anchor in (
            "intent-editor", "intent-outcome", "intent-criteria", "intent-constraints",
            "save-intent", "goal-status", "goal-outcome", "gap-title", "action-actor",
            "completion-evidence", "confirm-achievement", "acceptance-review", "acceptance-criteria",
        ):
            self.assertIn(f'id="{anchor}"', html)
        self.assertIn("目标已在其他页面更新，请核对最新版本后再保存。", state_script)
        self.assertIn("可信验证已通过，请核对验收标准", script)
        self.assertIn("workStateByProject", script)
        self.assertNotIn("innerHTML", script)

    def test_intent_drafts_and_confirmations_are_project_and_version_scoped(self):
        html = (ROOT / "console/index.html").read_text(encoding="utf-8")
        script = (ROOT / "console/app.js").read_text(encoding="utf-8")
        state_script = (ROOT / "console/state.js").read_text(encoding="utf-8")
        self.assertIn('id="reload-latest-intent"', html)
        self.assertIn('id="action-started"', html)
        self.assertLess(html.index('src="./state.js"'), html.index('src="./app.js"'))
        self.assertIn("draft: null", state_script)
        self.assertIn("baseVersion", state_script)
        self.assertIn("updateProjectDraft", script)
        self.assertIn("isWorkViewConsistent", script)
        self.assertIn("if (currentAction(project)) return;", script)
        self.assertIn("State.openEditor", script)
        self.assertIn("State.reloadLatestDraft", script)
        self.assertIn("State.beginIntentSave", script)
        self.assertIn("当前版本只读展示这个决定", script)
        self.assertIn('"evidence_advanced"', script)

    def test_intent_submit_clears_validation_state_on_every_editor_field(self):
        script = (ROOT / "console/app.js").read_text(encoding="utf-8")

        self.assertIn(
            "for (const field of [intentOutcome, intentCriteria, intentConstraints])",
            script,
        )

    def test_javascript_prefers_recent_activity_and_preserves_manual_selection(self):
        script = (ROOT / "console/app.js").read_text(encoding="utf-8")
        self.assertIn('const SELECTED_PROJECT_KEY = "software-factory:selected-project"', script)
        self.assertIn("function preferredProjectId", script)
        self.assertIn("function compareProjectActivity", script)
        self.assertIn("project.active_run_id", script)
        self.assertIn('project.activity_status === "recent"', script)
        self.assertIn("sessionStorage.getItem(SELECTED_PROJECT_KEY)", script)
        self.assertIn("sessionStorage.setItem(SELECTED_PROJECT_KEY, project.project_id)", script)
        self.assertIn("const storedProject = storedProjectId(projects)", script)
        self.assertNotIn("selectedProjectId = projects[0] ? projects[0].project_id : null", script)

    def test_javascript_drives_surface_and_spine_states(self):
        script = (ROOT / "console/app.js").read_text(encoding="utf-8")
        self.assertIn("function setSurfaceState", script)
        self.assertIn("function renderSystemSpine", script)
        self.assertIn("function setCurrentSpineRole", script)
        self.assertIn('step.setAttribute("aria-current", "step")', script)
        self.assertIn("setCurrentSpineRole(null)", script)
        self.assertIn("evidence.replaceChildren();", script)
        self.assertIn('setSurfaceState("loading")', script)
        self.assertIn('setSurfaceState("ready")', script)
        self.assertIn('setSurfaceState("running")', script)
        self.assertIn("function renderFacts", script)
        self.assertIn("function renderAgentRecord", script)
        self.assertIn("function renderWorkSummary", script)
        self.assertIn("project.work_summary", script)
        self.assertIn("主管 Agent 尚未运行", script)
        self.assertIn("Observer 事件、Git 元数据、验证退出码", script)
        self.assertIn("activity.error_code", script)
        self.assertIn('activity.status === "queued"', script)

    def test_motion_is_orchestrated_and_accessible(self):
        css = (ROOT / "console/app.css").read_text(encoding="utf-8")
        self.assertEqual(css.count("@keyframes console-enter"), 1)
        self.assertIn("prefers-reduced-motion: reduce", css)
        self.assertIn("animation: none", css)
        self.assertIn("transform: none !important", css)

    def test_loading_state_has_stable_skeleton_blocks(self):
        html = (ROOT / "console/index.html").read_text(encoding="utf-8")
        css = (ROOT / "console/app.css").read_text(encoding="utf-8")
        self.assertIn('aria-busy="true"', html)
        self.assertIn('.factory-shell[data-state="loading"] h1', css)
        self.assertIn('.factory-shell[data-state="loading"] .fact-list', css)
        self.assertRegex(css, re.compile(r"^h1 \{[^}]*-webkit-line-clamp: 3", re.M | re.S))
        self.assertRegex(css, re.compile(r"^h1 \{[^}]*min-height: clamp\(7rem, 13vw, 14rem\)", re.M | re.S))
        self.assertRegex(css, re.compile(r"^\.factory-shell\[data-state=\"loading\"\] \.fact-list \{[^}]*min-height: 114px", re.M | re.S))

    def test_visible_copy_has_no_banned_dashes(self):
        for path in (ROOT / "console").glob("*"):
            self.assertNotRegex(path.read_text(encoding="utf-8"), "[—–]")


if __name__ == "__main__":
    unittest.main()

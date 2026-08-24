from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.models import (
    AgentHostKind,
    CandidateProposal,
    GoalPlusNextAction,
    GoalPlusSpecDraft,
    GoalPlusSpecDraftInput,
    GoalPlusTriage,
    SearchSpec,
    ToolizationDecision,
    VerifierInvalidationReason,
)
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.runtime import FileSearchRuntime


class SearchTools:
    """JSON-friendly tool layer shared by tests and the MCP server."""

    def __init__(self, runtime: FileSearchRuntime) -> None:
        self.runtime = runtime

    def search_freeze_spec(
        self,
        spec: dict[str, Any] | SearchSpec,
        verifier_artifact_paths: list[str],
    ) -> dict[str, Any]:
        parsed_spec = spec if isinstance(spec, SearchSpec) else SearchSpec.model_validate(spec)
        frozen = self.runtime.freeze_spec(
            parsed_spec,
            [Path(path) for path in verifier_artifact_paths],
        )
        return frozen.model_dump(mode="json")

    def search_create(
        self,
        frozen_spec_id: str,
        source_run_id: str | None = None,
    ) -> dict[str, str]:
        return {
            "run_id": self.runtime.create_run(
                frozen_spec_id,
                source_run_id=source_run_id,
            )
        }

    def goal_plus_list_models(
        self,
        host: AgentHostKind,
        query: str | None = None,
    ) -> dict[str, Any]:
        return self.runtime.list_available_models(host, query)

    def search_invalidate_run(
        self,
        run_id: str,
        reason: VerifierInvalidationReason,
        summary: str,
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.runtime.invalidate_run(
            run_id,
            reason=reason,
            summary=summary,
            evidence=evidence,
        ).model_dump(mode="json")

    def search_status(self, run_id: str) -> dict[str, Any]:
        return self.runtime.status(run_id).model_dump(mode="json")

    def search_recover_pi_thinkthread(
        self,
        run_id: str,
    ) -> dict[str, Any]:
        return self.runtime.recover_pi_thinkthread_snapshot_requests(run_id)

    def search_list_history(
        self,
        run_id: str,
        top_n: int = 5,
        sort_by: str = "score",
    ) -> dict[str, Any]:
        return self.runtime.list_history(run_id, top_n=top_n, sort_by=sort_by)

    def search_plan_next(self, run_id: str, requested_k: int = 4) -> dict[str, Any]:
        return self.runtime.plan_next(run_id, requested_k=requested_k).model_dump(mode="json")

    def search_start_batch(
        self,
        run_id: str,
        plan_id: str,
        proposals: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        parsed_proposals = (
            [CandidateProposal.model_validate(proposal) for proposal in proposals]
            if proposals is not None
            else None
        )
        return [
            task.model_dump(mode="json")
            for task in self.runtime.start_batch(run_id, plan_id, parsed_proposals)
        ]

    def search_start_agent_session(
        self,
        run_id: str,
        candidate_id: str,
        directive: dict[str, Any] | str | None = None,
        worker_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.runtime.start_agent_session(
            run_id=run_id,
            candidate_id=candidate_id,
            directive=directive,
            worker_budget=worker_budget,
        ).model_dump(mode="json")

    def search_redispatch_candidate(
        self,
        run_id: str,
        candidate_id: str,
        worker_agent_type: str | None = None,
        worker_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.runtime.redispatch_candidate(
            run_id=run_id,
            candidate_id=candidate_id,
            worker_agent_type=worker_agent_type,
            worker_budget=worker_budget,
        ).model_dump(mode="json")

    def search_bind_agent_handle(
        self,
        agent_session_id: str,
        handle: dict[str, Any],
    ) -> dict[str, Any]:
        return self.runtime.bind_agent_handle(
            agent_session_id=agent_session_id,
            handle=handle,
        ).model_dump(mode="json")

    def search_continue_agent_session(
        self,
        agent_session_id: str,
        worker_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.runtime.continue_agent_session(
            agent_session_id=agent_session_id,
            worker_budget=worker_budget,
        ).model_dump(mode="json")

    def search_get_agent_context(self, agent_session_id: str) -> dict[str, Any]:
        return self.runtime.get_agent_context(agent_session_id)

    def search_get_global_evidence(
        self,
        agent_session_id: str,
    ) -> list[dict[str, Any]]:
        return self.runtime.get_global_evidence(agent_session_id)

    def search_copy_shared_tool(
        self,
        agent_session_id: str,
        tool_id: str,
        snapshot_hash: str,
    ) -> dict[str, Any]:
        return self.runtime.copy_shared_tool(
            agent_session_id,
            tool_id,
            snapshot_hash,
        )

    def search_stage_shared_tool(
        self,
        agent_session_id: str,
        name: str,
        summary: str,
        entrypoint: str,
        candidate_relative_source_paths: list[str],
    ) -> dict[str, Any]:
        return self.runtime.stage_shared_tool(
            agent_session_id=agent_session_id,
            name=name,
            summary=summary,
            entrypoint=entrypoint,
            candidate_relative_source_paths=candidate_relative_source_paths,
        )

    def search_get_evidence_detail(
        self,
        agent_session_id: str,
        candidate_id: str,
        iteration: int,
    ) -> dict[str, Any]:
        return self.runtime.get_evidence_detail(
            agent_session_id,
            candidate_id,
            iteration,
        )

    def search_get_agent_observability(self, agent_session_id: str) -> dict[str, Any]:
        return self.runtime.get_agent_observability(agent_session_id)

    def search_run_verifier(
        self,
        run_id: str,
        candidate_id: str,
        scope: Literal["process", "promotion"] = "process",
        agent_session_id: str | None = None,
        hypothesis: str | None = None,
        toolization_decision: ToolizationDecision | dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        verifier_kwargs: dict[str, Any] = {
            "scope": scope,
            "agent_session_id": agent_session_id,
            "hypothesis": hypothesis,
            "toolization_decision": toolization_decision,
        }
        if idempotency_key is not None:
            verifier_kwargs["idempotency_key"] = idempotency_key
        report = self.runtime.run_verifier(
            run_id,
            candidate_id,
            **verifier_kwargs,
        )
        result = report.model_dump(mode="json")
        if scope != "process" or agent_session_id is None:
            return result
        try:
            if not self.runtime.should_inject_global_evidence_after_verifier(run_id):
                return result
            snapshot = self.runtime.get_global_evidence(agent_session_id)
        except Exception as exc:
            result["global_evidence_injected"] = False
            result["global_evidence_warning"] = (
                f"Global Evidence injection failed: {type(exc).__name__}: {exc}"
            )
            return result
        result["global_evidence_snapshot"] = snapshot
        result["global_evidence_injected"] = True
        result["global_evidence_entry_count"] = len(snapshot)
        return result

    def search_list_iterations(
        self,
        run_id: str,
        candidate_id: str,
    ) -> list[dict[str, Any]]:
        return self.runtime.list_iterations(run_id, candidate_id)

    def search_select(self, run_id: str) -> dict[str, Any]:
        return self.runtime.select(run_id)

    def search_report(self, run_id: str) -> dict[str, str]:
        report_path = self.runtime.report(run_id)
        return {
            "report_path": str(report_path),
            "html_report_path": str(report_path.with_suffix(".html")),
        }

    def search_promote(self, run_id: str, candidate_id: str) -> dict[str, str]:
        return {"artifact_path": str(self.runtime.promote(run_id, candidate_id))}

    def goal_plus_monitor_snapshot(
        self,
        goal_plus_id: str | None = None,
        run_id: str | None = None,
        stale_after_seconds: int = 600,
    ) -> dict[str, Any]:
        return goal_plus_monitor_snapshot(
            root_dir=self.runtime.root_dir,
            goal_plus_id=goal_plus_id,
            run_id=run_id,
            stale_after_seconds=stale_after_seconds,
        )


class GoalPlusTools:
    """JSON-friendly goal-plus tool layer shared by tests and the MCP server."""

    def __init__(self, runtime: FileGoalPlusRuntime) -> None:
        self.runtime = runtime

    def goal_plus_create(
        self,
        raw_goal: str,
        source_path: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.runtime.create_goal(
            raw_goal=raw_goal,
            source_path=source_path,
            policy=policy,
        ).model_dump(mode="json")

    def goal_plus_status(self, goal_plus_id: str) -> dict[str, Any]:
        record = self.runtime.status(goal_plus_id)
        payload = record.model_dump(mode="json")
        payload["search_tasks_total"] = len(record.search_tasks)
        payload["current_search_run_id"] = (
            record.linked_search.run_id if record.linked_search is not None else None
        )
        payload["evidence_log"] = self.runtime.list_events(goal_plus_id)
        return payload

    def goal_plus_update_goal(
        self,
        goal_plus_id: str,
        raw_goal: str,
        expected_revision: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return self.runtime.update_goal(
            goal_plus_id,
            raw_goal=raw_goal,
            expected_revision=expected_revision,
            reason=reason,
        ).model_dump(mode="json")

    def goal_plus_record_triage(
        self,
        goal_plus_id: str,
        triage: dict[str, Any],
    ) -> dict[str, Any]:
        return self.runtime.record_triage(
            goal_plus_id,
            GoalPlusTriage.model_validate(triage),
        ).model_dump(mode="json")

    def goal_plus_save_spec_draft(
        self,
        goal_plus_id: str,
        spec_draft: dict[str, Any] | GoalPlusSpecDraft,
    ) -> dict[str, Any]:
        parsed_draft = (
            spec_draft
            if isinstance(spec_draft, GoalPlusSpecDraft)
            else GoalPlusSpecDraftInput.model_validate(spec_draft)
        )
        return self.runtime.save_spec_draft(
            goal_plus_id,
            parsed_draft,
        ).model_dump(mode="json")

    def goal_plus_link_search_run(
        self,
        goal_plus_id: str,
        frozen_spec_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        return self.runtime.link_search_run(
            goal_plus_id,
            frozen_spec_id,
            run_id,
        ).model_dump(mode="json")

    def goal_plus_record_search_result(
        self,
        goal_plus_id: str,
        run_id: str,
        selected_candidate_id: str | None = None,
        report_path: str | None = None,
        promotion_artifact_path: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        return self.runtime.record_search_result(
            goal_plus_id,
            run_id=run_id,
            selected_candidate_id=selected_candidate_id,
            report_path=report_path,
            promotion_artifact_path=promotion_artifact_path,
            summary=summary,
        ).model_dump(mode="json")

    def goal_plus_prepare_final_check(
        self,
        goal_plus_id: str,
        checker_host: str,
    ) -> dict[str, Any]:
        return self.runtime.prepare_final_check(
            goal_plus_id,
            checker_host=checker_host,  # type: ignore[arg-type]
        )

    def goal_plus_submit_final_check(
        self,
        goal_plus_id: str,
        check_id: str,
        goal_revision: int,
        verdict: str,
        summary: str,
        findings: list[dict[str, Any]] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        checker_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.runtime.submit_final_check(
            goal_plus_id,
            check_id=check_id,
            goal_revision=goal_revision,
            verdict=verdict,
            summary=summary,
            findings=findings,
            evidence=evidence,
            checker_metadata=checker_metadata,
        ).model_dump(mode="json")

    def goal_plus_set_status(
        self,
        goal_plus_id: str,
        status: str,
        reason: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        next_action: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parsed_next_action = (
            GoalPlusNextAction.model_validate(next_action)
            if next_action is not None
            else None
        )
        return self.runtime.set_status(
            goal_plus_id,
            status=status,  # type: ignore[arg-type]
            reason=reason,
            evidence=evidence,
            next_action=parsed_next_action,
        ).model_dump(mode="json")

    def goal_plus_gate(
        self,
        goal_plus_id: str,
        event: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return self.runtime.gate(
            goal_plus_id,
            event=event,  # type: ignore[arg-type]
            context=context,
        ).model_dump(mode="json")

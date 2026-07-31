"""The step executor — where an attempt actually becomes work.

This is the seam the earlier milestones were built around. Everything below has
been usable in isolation; this is what wires them into one attempt:

    condition → worktree → agent → commit → gates → merge → outcome

Each stage is optional. A run configured without a
:class:`~orchestrator.git_manager.workspace.WorkspaceManager` executes the agent
in a plain directory and skips committing; one without a
:class:`~orchestrator.test_runner.runner.TestRunner` skips gating. That is not
laxity — it is what makes the executor testable stage by stage, and it gives an
operator a genuine dry-run mode.

Three rules the executor exists to enforce:

* **The agent never accepts its own work.** It reports; gates decide
  (``docs/020_ARCHITECTURE`` §3.1). A successful agent run whose gate is red is
  a failed attempt.
* **A broken harness is not a failing test.** A gate that errored comes back as
  a *retryable* failure with feedback that says so, because an attempt told
  "tests failed" when the runner is broken will start editing tests (FR-4.4).
* **Failed work is preserved.** The worktree survives so it can be inspected;
  only a successful attempt's tree is a candidate for cleanup (FR-2.7).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from orchestrator.adapter.base import AgentPort
from orchestrator.agent.memory import TranscriptEntry
from orchestrator.agent.model import AttemptResult, AttemptStatus, BudgetLedger, TaskSpec
from orchestrator.agent.tools import ToolContext
from orchestrator.core.events import OrchestratorError, RunId, TaskId
from orchestrator.git_manager.commit import CommitManager
from orchestrator.git_manager.merge import MergeManager, MergeResult, MergeStatus
from orchestrator.git_manager.workspace import Workspace, WorkspaceManager
from orchestrator.task.dispatcher import AttemptOutcome
from orchestrator.task.model import Task
from orchestrator.test_runner.base import GateRunner, Outcome, SuiteSpec, Verdict
from orchestrator.test_runner.runner import aggregate
from orchestrator.workflow.definition import ConditionContext, WorkflowPlan
from orchestrator.workflow.progress import EventEmitter

__all__ = ["ExecutionServices", "StepExecutor", "WorkflowExecutionError"]


class WorkflowExecutionError(OrchestratorError):
    """The executor was asked to run an attempt it cannot run."""

    code = "workflow_execution"
    retryable = False


#: Attempt statuses that mean the agent produced something worth gating.
_WORTH_GATING = frozenset({AttemptStatus.SUCCEEDED, AttemptStatus.BUDGET_EXHAUSTED})

#: Agent failures that are worth another attempt.
_RETRYABLE_AGENT_STATUSES = frozenset(
    {AttemptStatus.BUDGET_EXHAUSTED, AttemptStatus.ERRORED}
)


def _write_transcript(path: Path, entries: Sequence[TranscriptEntry]) -> None:
    """Write one attempt's transcript as JSONL (FR-5.3).

    A plain file, one JSON object per line, so a transcript can be read,
    grepped, and diffed with ordinary tools — the reason `docs/020` §5 keeps
    transcripts out of SQLite.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")


@dataclass(frozen=True, slots=True)
class ExecutionServices:
    """The collaborators an executor uses, each optional.

    Attributes:
        workspaces: Creates an isolated worktree per attempt. Without one, the
            agent runs in ``fallback_root`` and nothing is committed.
        commits: Commits the agent's changes. Requires ``workspaces``.
        merges: Merges a passing attempt onto the run's integration branch.
        gates: Runs the project's checks. Any
            :class:`~orchestrator.test_runner.base.GateRunner` will do — the
            pytest runner, a build gate, anything that turns a directory and a
            spec into a verdict. Without one, nothing is verified and the
            outcome says so.
        fallback_root: Where to run when there is no workspace manager.
        transcripts: Root directory for durable attempt transcripts (FR-5.3).
            Each attempt writes ``<root>/<run>/<task>/<attempt>.jsonl``; without
            one, no transcript is written — the gap the spec's §5 path
            described for every build before this field existed.
    """

    workspaces: WorkspaceManager | None = None
    commits: CommitManager | None = None
    merges: MergeManager | None = None
    gates: GateRunner | None = None
    fallback_root: Path | None = None
    transcripts: Path | None = None

    def __post_init__(self) -> None:
        """Refuse a combination that could not run."""
        if self.commits is not None and self.workspaces is None:
            raise ValueError(
                "a commit manager needs a workspace manager: there is nothing to "
                "commit without a worktree"
            )
        if self.workspaces is None and self.fallback_root is None:
            raise ValueError(
                "provide either a workspace manager or a fallback_root to run in"
            )


class StepExecutor:
    """Turns one task attempt into an outcome the dispatcher understands."""

    __slots__ = (
        "_emitter",
        "_feedback",
        "_gate_specs",
        "_merge_lock",
        "_plan",
        "_run_id",
        "_runtime",
        "_services",
        "_skipped",
        "_succeeded",
        "_workspaces_by_task",
    )

    def __init__(
        self,
        runtime: AgentPort,
        plan: WorkflowPlan,
        *,
        run_id: RunId,
        services: ExecutionServices,
        emitter: EventEmitter | None = None,
        gate_specs: Mapping[str, SuiteSpec] | None = None,
    ) -> None:
        """Create the executor.

        Args:
            runtime: Any :class:`~orchestrator.adapter.base.AgentPort` — our
                tool loop, or an external harness. Provider-agnostic by
                construction, and now backend-agnostic too.
            plan: The compiled workflow, for step names and conditions.
            run_id: The run these attempts belong to.
            services: The optional collaborators.
            emitter: Records gate verdicts and other events.
            gate_specs: How to run each named gate. A gate with no spec is
                skipped with a recorded reason rather than silently passing.
        """
        self._runtime = runtime
        self._plan = plan
        self._run_id = run_id
        self._services = services
        self._emitter = emitter
        self._gate_specs = dict(gate_specs or {})
        self._succeeded: set[str] = set()
        self._skipped: set[str] = set()
        self._feedback: dict[TaskId, list[str]] = {}
        self._workspaces_by_task: dict[TaskId, Workspace] = {}
        self._merge_lock = asyncio.Lock()

    # ------------------------------------------------------------- accessors

    @property
    def skipped_steps(self) -> frozenset[str]:
        """Steps that succeeded only because their condition was false."""
        return frozenset(self._skipped)

    @property
    def completed_steps(self) -> frozenset[str]:
        """Steps that reached a successful outcome, skipped ones included."""
        return frozenset(self._succeeded)

    @property
    def workspaces(self) -> Mapping[TaskId, Workspace]:
        """The worktree created for each task's most recent attempt."""
        return dict(self._workspaces_by_task)

    def seed_completed(self, steps: Iterable[str]) -> None:
        """Mark steps as already successful, for a resumed run.

        Without this a resumed run would evaluate conditions against an empty
        history and prune branches whose premise was in fact satisfied by the
        earlier process.
        """
        self._succeeded.update(steps)

    def feedback_for(self, task_id: TaskId) -> tuple[str, ...]:
        """Return the feedback accumulated for a task's next attempt."""
        return tuple(self._feedback.get(task_id, ()))

    # ------------------------------------------------------------- execution

    async def __call__(self, task: Task, attempt: int) -> AttemptOutcome:
        """Run one attempt at one task.

        Args:
            task: The task to attempt.
            attempt: The 1-based attempt number.

        Returns:
            The outcome, carrying whether a retry could plausibly help.
        """
        step = self._plan.step_for(task.id)

        if step.condition is not None:
            ctx = ConditionContext(
                succeeded=frozenset(self._succeeded), skipped=frozenset(self._skipped)
            )
            if not step.condition.evaluate(ctx):
                self._skipped.add(step.name)
                self._succeeded.add(step.name)
                return AttemptOutcome.success(
                    skipped=True, condition=step.condition.describe()
                )

        workspace, root = await self._prepare(task, attempt)
        if workspace is not None:
            self._workspaces_by_task[task.id] = workspace

        result = await self._run_agent(task, attempt, root)
        # Usage rides on every outcome that had an agent run behind it. Before
        # this, the runtime's carefully accumulated counts died here and every
        # served attempt recorded zero tokens (FR-5.4, broken end to end).
        # `tokens_estimated` travels with the numbers so no surface downstream
        # can present an approximation as a measurement.
        usage = {
            "tokens_in": result.usage.input_tokens,
            "tokens_out": result.usage.output_tokens,
            "cost_usd": result.usage.cost_usd,
            "tokens_estimated": result.usage.estimated,
        }
        # OP-014. Two things the harness can now observe that belong in the
        # permanent record rather than in executor-private detail:
        #
        #   permission_denials - the run-3 failure mode. An agent refused a
        #   tool, changed nothing, exited zero, and the gate verified an
        #   unchanged tree. The no-op guard fails that attempt now, but without
        #   this the *reason* is still nowhere a reviewer can find it.
        #
        #   notional_cost_usd - what the call would have cost at list API
        #   prices. Deliberately NOT `cost_usd`: under a subscription the
        #   marginal cost was zero, and reporting a charge that did not happen
        #   is the $0.00 error inverted.
        observed = {
            key: result.detail[key]
            for key in ("permission_denials", "notional_cost_usd")
            if key in result.detail
        }

        if result.status not in _WORTH_GATING:
            self._remember(task.id, f"attempt {attempt}: {result.summary}")
            return AttemptOutcome.failure(
                result.error_code or result.status.value,
                retryable=result.status in _RETRYABLE_AGENT_STATUSES,
                summary=result.summary,
                changed_files=list(result.changed_files),
                usage=usage,
                **observed,
            )

        # A green gate over unmodified code verifies the baseline, not the
        # task. The first real harness run passed every gate having changed
        # nothing — the agent was blocked, said so, exited zero, and the
        # unchanged suite went green. "An agent cannot mark itself green" has
        # a hole in it when the agent does nothing at all; this closes it.
        # Per-step, because a verification step legitimately changes nothing.
        if step.expects_changes and not result.changed_files:
            self._remember(
                task.id,
                f"attempt {attempt} changed no files. The task expects the "
                "worktree to be modified. If you were blocked from editing, "
                "say what blocked you; do not report success without work.",
            )
            return AttemptOutcome.failure(
                "no_op",
                # Worth another try: the agent may have been blocked by
                # something transient, and now it is told what went wrong.
                retryable=True,
                summary=result.summary,
                changed_files=[],
                usage=usage,
                **observed,
            )

        committed = await self._commit(workspace, task, attempt, result)
        verdicts = await self._gate(task, root)

        if verdicts:
            worst = aggregate(verdicts)
            if worst is not Outcome.PASSED:
                self._remember(task.id, self._gate_feedback(verdicts))
                return AttemptOutcome.failure(
                    f"gate_{worst.value}",
                    # A broken harness is worth another try; a red suite is only
                    # worth one if the agent might fix it, which it might.
                    retryable=True,
                    gate=worst.value,
                    failing=[name for v in verdicts for name in v.failed_names],
                    changed_files=list(result.changed_files),
                    usage=usage,
                    **observed,
                )

        merge = await self._merge(workspace)

        # A merge that did not land is a failed attempt, not a footnote on a
        # successful one. This returned a bool nobody inspected until a real
        # run reported 2/2 healthy while the integration branch — the artefact
        # the operator is told to review — held only half the work, and the
        # second step's commit sat orphaned on its attempt branch
        # (PROJECT_STATUS D-2d, FR-3.5).
        # NOTHING_TO_MERGE is not a failed merge: it means the attempt produced
        # no commit at all, which is the no-op case above and is governed by
        # the step's own `expects_changes` policy. Failing it here would break
        # every legitimately diff-free step. What must never pass silently is
        # work that exists and cannot land.
        if (
            merge is not None
            and not merge.ok
            and merge.status is not MergeStatus.NOTHING_TO_MERGE
        ):
            conflicted = list(merge.conflicted_paths)
            detail = (
                "conflicting with work already on the integration branch in: "
                + ", ".join(conflicted)
                if conflicted
                else f"the merge did not land ({merge.status.value})"
            )
            self._remember(
                task.id,
                f"attempt {attempt} produced work that could not be merged — "
                f"{detail}. The next attempt starts from the integration "
                "branch as it now stands, so read those files first and apply "
                "your change on top of what is already there.",
            )
            return AttemptOutcome.failure(
                "merge_conflict" if merge.conflicted else "merge_failed",
                # Worth one more try: the retry branches from the integration
                # branch's current state, so the conflict cannot recur
                # unchanged, and it is told which files collided.
                retryable=True,
                summary=result.summary,
                merged=False,
                merge_status=merge.status.value,
                conflicted_paths=conflicted,
                changed_files=list(result.changed_files),
                usage=usage,
                **observed,
            )

        self._succeeded.add(step.name)
        return AttemptOutcome.success(
            summary=result.summary,
            changed_files=list(result.changed_files),
            committed=committed,
            merged=merge.ok if merge is not None else False,
            # OP-011: the *success* path was silent about the merge. D-2d made
            # a failure loud, but a healthy run's log carried no verdict at
            # all, so "did this step's work actually land?" could only be
            # answered by running git against the repository — which is the
            # question the event log exists to answer.
            merge_status=merge.status.value if merge is not None else "",
            gates=[v.outcome.value for v in verdicts],
            usage=usage,
            **observed,
        )

    # -------------------------------------------------------------- stages

    async def _prepare(
        self, task: Task, attempt: int
    ) -> tuple[Workspace | None, Path]:
        """Create the attempt's isolated worktree, or fall back to a directory."""
        manager = self._services.workspaces
        if manager is None:
            root = self._services.fallback_root
            if root is None:  # pragma: no cover - ExecutionServices guarantees it
                raise WorkflowExecutionError(
                    "no workspace manager and no fallback_root: there is "
                    "nowhere to run this attempt"
                )
            root.mkdir(parents=True, exist_ok=True)
            return None, root

        # Branch from the run's integration branch, not from HEAD. Two reasons,
        # and the second is the important one:
        #
        #   * a later step then *sees* what earlier steps merged, which is what
        #     "depends_on" is supposed to mean;
        #   * a retry after a merge conflict is rebased by construction — it
        #     starts from the branch it failed to merge into, so the conflict
        #     it hit cannot recur identically.
        #
        # Branching every attempt from HEAD is what made two steps editing one
        # file produce an unmergeable second commit (PROJECT_STATUS D-2d).
        start_point = "HEAD"
        merges = self._services.merges
        if merges is not None:
            # Under the same lock as the merge itself: `ensure` is
            # check-then-act on one ref, so two concurrent attempts preparing
            # at once both see it missing and the loser gets "reference
            # already exists". That is the identical race the merge lock was
            # introduced for — the lock guards the integration ref, not just
            # the merge stage.
            async with self._merge_lock:
                start_point = await merges.ensure_integration_branch(self._run_id)

        workspace = await manager.create(
            run_id=self._run_id,
            task_id=task.id,
            attempt=attempt,
            start_point=start_point,
        )
        return workspace, workspace.path

    async def _run_agent(
        self, task: Task, attempt: int, root: Path
    ) -> AttemptResult:
        """Run the agent, bounded by the task's own wall-clock budget."""
        step = self._plan.step_for(task.id)
        spec = TaskSpec(
            task_id=task.id,
            title=task.title,
            prompt=task.prompt,
            role=step.role,
            feedback=self.feedback_for(task.id),
            labels=task.labels,
        )
        ledger = BudgetLedger(task.budget)
        ctx = ToolContext(workspace_root=root)

        # The executor, not the runtime, knows which run, task, and attempt an
        # entry belongs to — one runtime serves every concurrent attempt — so
        # transcript routing lives here (FR-5.3).
        entries: list[TranscriptEntry] = []
        sink = entries.append if self._services.transcripts is not None else None

        try:
            result = await asyncio.wait_for(
                self._runtime.run(spec, ctx, ledger, transcript_sink=sink),
                timeout=task.budget.seconds,
            )
        except TimeoutError:
            # The budget's wall-clock axis, enforced from outside as well as
            # inside: a runtime wedged in a tool cannot check its own ledger.
            result = AttemptResult(
                status=AttemptStatus.BUDGET_EXHAUSTED,
                summary=(
                    f"the attempt exceeded its {task.budget.seconds:g}s wall-clock "
                    "budget and was stopped"
                ),
                error_code="timeout",
                detail={"axis": "seconds", "limit": task.budget.seconds},
            )

        if self._services.transcripts is not None and entries:
            path = (
                self._services.transcripts
                / str(self._run_id)
                / str(task.id)
                / f"{attempt}.jsonl"
            )
            # File I/O off the event loop; a transcript can be sizeable.
            await asyncio.to_thread(_write_transcript, path, entries)

        return result

    async def _commit(
        self,
        workspace: Workspace | None,
        task: Task,
        attempt: int,
        result: AttemptResult,
    ) -> bool:
        """Commit the attempt's work, if there is a repository to commit to."""
        commits = self._services.commits
        if commits is None or workspace is None:
            return False
        step = self._plan.step_for(task.id)
        outcome = await commits.commit_if_changed(
            workspace, f"{step.display_title}\n\n{result.summary}".strip()
        )
        return outcome.created

    async def _gate(self, task: Task, root: Path) -> tuple[Verdict, ...]:
        """Run every configured gate for a task."""
        runner = self._services.gates
        if runner is None or not task.gates:
            return ()

        verdicts: list[Verdict] = []
        for gate in task.gates:
            spec = self._gate_specs.get(gate.name)

            if spec is None:
                # A required gate with no suite verified nothing, and must not
                # read as a pass. An advisory one is genuinely optional.
                verdicts.append(
                    Verdict(
                        outcome=Outcome.SKIPPED if gate.required else Outcome.PASSED,
                        gate=gate.name,
                        reason="no suite is configured for this gate",
                    )
                )
                continue

            verdict = await runner.run(root, spec)
            if self._emitter is not None:
                self._emitter.gate_evaluated(
                    task.id,
                    None,
                    gate=verdict.gate,
                    verdict=verdict.outcome.value,
                    required=gate.required,
                )

            if gate.required:
                verdicts.append(verdict)
            else:
                # An advisory gate reports without blocking, so its real outcome
                # is preserved in the reason rather than thrown away.
                verdicts.append(
                    Verdict(
                        outcome=Outcome.PASSED,
                        gate=verdict.gate,
                        cases=verdict.cases,
                        counts=verdict.counts,
                        duration_s=verdict.duration_s,
                        reason=f"advisory: {verdict.summary()}",
                    )
                )
        return tuple(verdicts)

    async def _merge(self, workspace: Workspace | None) -> MergeResult | None:
        """Merge a passing attempt onto the run's integration branch.

        Serialized behind the executor's lock, which guards **every** operation
        on the run's integration ref — this merge and the ``ensure`` in
        :meth:`_prepare`. Attempts run in parallel, integration does not
        (``docs/020_ARCHITECTURE`` §4, rule 1): the rule was documented in M0
        and unimplemented until two concurrent passing attempts raced inside
        ``ensure_integration_branch``, and reintroduced the moment ``_prepare``
        started calling ``ensure`` outside the lock. One executor serves a
        whole run, so one lock serializes that run's integration.
        """
        merges = self._services.merges
        if merges is None or workspace is None:
            return None
        async with self._merge_lock:
            return await merges.integrate_attempt(
                run_id=self._run_id, source=workspace.branch
            )

    # ------------------------------------------------------------- feedback

    def _remember(self, task_id: TaskId, note: str) -> None:
        """Record why an attempt failed, for the next one to read (FR-2.5)."""
        if note.strip():
            self._feedback.setdefault(task_id, []).append(note.strip())

    @staticmethod
    def _gate_feedback(verdicts: tuple[Verdict, ...]) -> str:
        """Render gate verdicts as feedback for the next attempt."""
        return "\n\n".join(
            verdict.feedback() for verdict in verdicts if verdict.blocks
        )

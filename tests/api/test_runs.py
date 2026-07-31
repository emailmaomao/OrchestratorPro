"""Tests for run and task endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator.core.events import EventType, RunId

from tests.api.conftest import new_run, new_task


class TestCreateRun:
    """Declaring a run."""

    def test_a_run_is_created(self, client: TestClient) -> None:
        response = client.post("/runs", json={"goal": "migrate the client"})

        assert response.status_code == 201
        body = response.json()
        assert body["goal"] == "migrate the client"
        assert body["status"] == "created"
        assert body["tasks"] == []

    def test_the_identifier_is_usable_immediately(self, client: TestClient) -> None:
        run_id = new_run(client)
        assert client.get(f"/runs/{run_id}").status_code == 200

    def test_creation_is_recorded_in_the_log(
        self, client: TestClient, state_of: object
    ) -> None:
        """Nothing the API does may exist only in memory."""
        run_id = new_run(client, "recorded")
        state = state_of(client)  # type: ignore[operator]
        events = state.store.events.read_run(RunId(run_id))

        assert [event.type for event in events] == [EventType.RUN_CREATED]
        assert events[0].payload["goal"] == "recorded"

    def test_an_empty_goal_is_refused(self, client: TestClient) -> None:
        assert client.post("/runs", json={"goal": "  "}).status_code == 422

    def test_two_runs_get_different_identifiers(self, client: TestClient) -> None:
        assert new_run(client) != new_run(client)


class TestListRuns:
    """Listing runs."""

    def test_an_empty_server_lists_nothing(self, client: TestClient) -> None:
        assert client.get("/runs").json() == []

    def test_runs_are_listed_newest_first(self, client: TestClient) -> None:
        first = new_run(client, "first")
        second = new_run(client, "second")
        listed = [run["id"] for run in client.get("/runs").json()]

        assert listed == [second, first]

    def test_the_limit_is_honoured(self, client: TestClient) -> None:
        for _ in range(3):
            new_run(client)
        assert len(client.get("/runs?limit=2").json()) == 2

    def test_an_absurd_limit_is_refused(self, client: TestClient) -> None:
        assert client.get("/runs?limit=0").status_code == 422

    def test_the_summary_counts_tasks(self, client: TestClient) -> None:
        run_id = new_run(client)
        new_task(client, run_id)
        assert client.get("/runs").json()[0]["tasks"] == 1

    def test_inactive_runs_can_be_filtered_out(self, client: TestClient) -> None:
        new_run(client)
        assert client.get("/runs?active=true").json() == []
        assert len(client.get("/runs?active=false").json()) == 1


class TestGetRun:
    """Reading one run."""

    def test_an_unknown_run_is_a_404(self, client: TestClient) -> None:
        assert client.get(f"/runs/{RunId.generate()}").status_code == 404

    def test_a_run_reports_its_tasks(self, client: TestClient) -> None:
        run_id = new_run(client)
        new_task(client, run_id, title="one")
        new_task(client, run_id, title="two")
        body = client.get(f"/runs/{run_id}").json()

        assert len(body["tasks"]) == 2
        assert body["state_counts"] == {"pending": 2}

    def test_usage_totals_are_reported(self, client: TestClient) -> None:
        body = client.get(f"/runs/{new_run(client)}").json()

        assert body["usage"] == {
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": None,
            # OP-004: totals declare whether they are measurements.
            "tokens_estimated": False,
        }

    def test_the_event_count_grows_with_activity(self, client: TestClient) -> None:
        run_id = new_run(client)
        before = client.get(f"/runs/{run_id}").json()["event_count"]
        new_task(client, run_id)
        after = client.get(f"/runs/{run_id}").json()["event_count"]

        assert after == before + 1


class TestRunStatus:
    """The compact status a poller wants."""

    def test_an_empty_run_is_zero_percent(self, client: TestClient) -> None:
        body = client.get(f"/runs/{new_run(client)}/status").json()

        assert body["total"] == 0
        assert body["percent"] == 0.0
        assert body["complete"] is False

    def test_pending_tasks_are_counted(self, client: TestClient) -> None:
        run_id = new_run(client)
        new_task(client, run_id)
        body = client.get(f"/runs/{run_id}/status").json()

        assert body["total"] == 1
        assert body["pending"] == 1

    def test_an_idle_run_is_not_active(self, client: TestClient) -> None:
        assert client.get(f"/runs/{new_run(client)}/status").json()["active"] is False

    def test_the_summary_reads_naturally(self, client: TestClient) -> None:
        run_id = new_run(client)
        new_task(client, run_id)
        assert "0/1" in client.get(f"/runs/{run_id}/status").json()["summary"]


class TestCancel:
    """Cancelling."""

    def test_cancelling_an_idle_run_is_a_conflict(self, client: TestClient) -> None:
        """Writing a cancellation into a run that already stopped would mislead."""
        response = client.post(f"/runs/{new_run(client)}/cancel")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"

    def test_cancelling_an_unknown_run_is_a_404(self, client: TestClient) -> None:
        assert client.post(f"/runs/{RunId.generate()}/cancel").status_code == 404


class TestCreateTask:
    """Adding tasks to a run."""

    def test_a_task_is_created(self, client: TestClient) -> None:
        run_id = new_run(client)
        body = new_task(client, run_id, title="write it", prompt="do the thing")

        assert body["title"] == "write it"
        assert body["state"] == "pending"
        assert body["attempts_made"] == 0
        assert body["run_id"] == run_id

    def test_the_attempt_allowance_is_kept(self, client: TestClient) -> None:
        run_id = new_run(client)
        body = new_task(client, run_id, max_attempts=5)

        assert body["max_attempts"] == 5
        assert body["attempts_remaining"] == 5

    def test_dependencies_are_recorded(self, client: TestClient) -> None:
        run_id = new_run(client)
        first = new_task(client, run_id, title="first")
        second = new_task(client, run_id, title="second", depends_on=[first["id"]])

        assert second["depends_on"] == [first["id"]]

    def test_an_unknown_dependency_is_refused(self, client: TestClient) -> None:
        """A task whose dependency does not exist is stuck by construction."""
        run_id = new_run(client)
        response = client.post(
            f"/runs/{run_id}/tasks",
            json={"title": "t", "prompt": "p", "depends_on": [str(RunId.generate())]},
        )

        assert response.status_code == 404
        assert "depend on" in response.json()["error"]["message"]

    def test_adding_to_an_unknown_run_is_a_404(self, client: TestClient) -> None:
        response = client.post(
            f"/runs/{RunId.generate()}/tasks", json={"title": "t", "prompt": "p"}
        )
        assert response.status_code == 404

    def test_an_empty_prompt_is_refused(self, client: TestClient) -> None:
        run_id = new_run(client)
        response = client.post(f"/runs/{run_id}/tasks", json={"title": "t", "prompt": ""})
        assert response.status_code == 422

    def test_creation_is_recorded_in_the_log(
        self, client: TestClient, state_of: object
    ) -> None:
        run_id = new_run(client)
        task = new_task(client, run_id)
        state = state_of(client)  # type: ignore[operator]
        events = state.store.events.read_run(RunId(run_id))

        created = [e for e in events if e.type is EventType.TASK_CREATED]
        assert len(created) == 1
        assert str(created[0].task_id) == task["id"]


class TestListTasks:
    """Listing a run's tasks."""

    def test_tasks_are_listed(self, client: TestClient) -> None:
        run_id = new_run(client)
        new_task(client, run_id, title="one")
        new_task(client, run_id, title="two")

        assert len(client.get(f"/runs/{run_id}/tasks").json()) == 2

    def test_a_run_with_no_tasks_lists_nothing(self, client: TestClient) -> None:
        assert client.get(f"/runs/{new_run(client)}/tasks").json() == []

    def test_the_state_filter_works(self, client: TestClient) -> None:
        run_id = new_run(client)
        new_task(client, run_id)

        assert len(client.get(f"/runs/{run_id}/tasks?state=pending").json()) == 1
        assert client.get(f"/runs/{run_id}/tasks?state=succeeded").json() == []

    def test_listing_an_unknown_run_is_a_404(self, client: TestClient) -> None:
        assert client.get(f"/runs/{RunId.generate()}/tasks").status_code == 404


class TestGetTask:
    """Reading one task."""

    def test_a_task_is_returned(self, client: TestClient) -> None:
        run_id = new_run(client)
        task = new_task(client, run_id, title="specific")
        body = client.get(f"/runs/{run_id}/tasks/{task['id']}").json()

        assert body["title"] == "specific"

    def test_an_unknown_task_is_a_404(self, client: TestClient) -> None:
        from orchestrator.core.events import TaskId

        run_id = new_run(client)
        response = client.get(f"/runs/{run_id}/tasks/{TaskId.generate()}")

        assert response.status_code == 404


class TestUpdateTask:
    """Amending a task."""

    def test_a_pending_task_can_be_amended(self, client: TestClient) -> None:
        run_id = new_run(client)
        task = new_task(client, run_id, title="before")
        response = client.patch(
            f"/runs/{run_id}/tasks/{task['id']}", json={"title": "after"}
        )

        assert response.status_code == 200
        assert response.json()["title"] == "after"

    def test_unset_fields_are_left_alone(self, client: TestClient) -> None:
        run_id = new_run(client)
        task = new_task(client, run_id, title="keep", prompt="original")
        body = client.patch(
            f"/runs/{run_id}/tasks/{task['id']}", json={"title": "changed"}
        ).json()

        assert body["prompt"] == "original"

    def test_the_amendment_is_appended_not_applied(
        self, client: TestClient, state_of: object
    ) -> None:
        """The log is append-only; the earlier declaration stays in the record."""
        run_id = new_run(client)
        task = new_task(client, run_id, title="before")
        client.patch(f"/runs/{run_id}/tasks/{task['id']}", json={"title": "after"})

        state = state_of(client)  # type: ignore[operator]
        created = [
            event
            for event in state.store.events.read_run(RunId(run_id))
            if event.type is EventType.TASK_CREATED
        ]
        assert len(created) == 2
        assert created[0].payload["title"] == "before"
        assert created[1].payload["amended"] is True

    def test_the_identifier_does_not_change(self, client: TestClient) -> None:
        run_id = new_run(client)
        task = new_task(client, run_id)
        body = client.patch(
            f"/runs/{run_id}/tasks/{task['id']}", json={"prompt": "new"}
        ).json()

        assert body["id"] == task["id"]

    def test_dependencies_survive_an_amendment(self, client: TestClient) -> None:
        run_id = new_run(client)
        first = new_task(client, run_id, title="first")
        second = new_task(client, run_id, title="second", depends_on=[first["id"]])
        body = client.patch(
            f"/runs/{run_id}/tasks/{second['id']}", json={"title": "renamed"}
        ).json()

        assert body["depends_on"] == [first["id"]]

    def test_an_amendment_of_an_unknown_task_is_a_404(self, client: TestClient) -> None:
        from orchestrator.core.events import TaskId

        run_id = new_run(client)
        response = client.patch(
            f"/runs/{run_id}/tasks/{TaskId.generate()}", json={"title": "x"}
        )
        assert response.status_code == 404

    def test_a_finished_task_cannot_be_amended(
        self, client: TestClient, state_of: object
    ) -> None:
        """Its prompt is part of an attempt's record by then."""
        from orchestrator.core.events import Event, TaskId

        run_id = new_run(client)
        task = new_task(client, run_id)
        state = state_of(client)  # type: ignore[operator]
        state.record(
            Event.new(
                EventType.TASK_SUCCEEDED,
                run_id=RunId(run_id),
                task_id=TaskId(task["id"]),
            )
        )

        response = client.patch(
            f"/runs/{run_id}/tasks/{task['id']}", json={"title": "too late"}
        )
        assert response.status_code == 409
        assert "succeeded" in response.json()["error"]["message"]


class TestDeleteTask:
    """Retiring a task."""

    def test_a_pending_task_is_abandoned_not_erased(self, client: TestClient) -> None:
        run_id = new_run(client)
        task = new_task(client, run_id)
        response = client.delete(f"/runs/{run_id}/tasks/{task['id']}")

        assert response.status_code == 200
        assert response.json()["state"] == "abandoned"

    def test_it_is_still_listed_afterwards(self, client: TestClient) -> None:
        """Append-only means retired, not gone."""
        run_id = new_run(client)
        task = new_task(client, run_id)
        client.delete(f"/runs/{run_id}/tasks/{task['id']}")

        assert len(client.get(f"/runs/{run_id}/tasks").json()) == 1

    def test_a_started_task_cannot_be_retired(
        self, client: TestClient, state_of: object
    ) -> None:
        from orchestrator.core.events import Event, TaskId

        run_id = new_run(client)
        task = new_task(client, run_id)
        state = state_of(client)  # type: ignore[operator]
        state.record(
            Event.new(
                EventType.TASK_STARTED,
                run_id=RunId(run_id),
                task_id=TaskId(task["id"]),
                payload={"attempt": 1},
            )
        )

        response = client.delete(f"/runs/{run_id}/tasks/{task['id']}")
        assert response.status_code == 409
        assert "append-only" in response.json()["error"]["message"]

    def test_retiring_an_unknown_task_is_a_404(self, client: TestClient) -> None:
        from orchestrator.core.events import TaskId

        run_id = new_run(client)
        response = client.delete(f"/runs/{run_id}/tasks/{TaskId.generate()}")
        assert response.status_code == 404


class TestReadLog:
    """Reading a run's history without holding a stream open."""

    def test_the_log_is_returned_oldest_first(self, client: TestClient) -> None:
        run_id = new_run(client)
        new_task(client, run_id, title="one")
        new_task(client, run_id, title="two")

        events = client.get(f"/runs/{run_id}/log").json()

        assert [event["type"] for event in events] == [
            "run.created",
            "task.created",
            "task.created",
        ]
        assert events[1]["payload"]["title"] == "one"

    def test_an_empty_run_has_only_its_creation(self, client: TestClient) -> None:
        assert len(client.get(f"/runs/{new_run(client)}/log").json()) == 1

    def test_the_limit_is_honoured(self, client: TestClient) -> None:
        run_id = new_run(client)
        for index in range(4):
            new_task(client, run_id, title=f"task {index}")

        assert len(client.get(f"/runs/{run_id}/log?limit=2").json()) == 2

    def test_paging_from_the_last_identifier_is_stable(self, client: TestClient) -> None:
        """Identifiers sort in creation order, so paging works mid-run."""
        run_id = new_run(client)
        new_task(client, run_id, title="one")
        new_task(client, run_id, title="two")

        first = client.get(f"/runs/{run_id}/log?limit=2").json()
        rest = client.get(f"/runs/{run_id}/log?after={first[-1]['id']}").json()

        assert [event["id"] for event in first] != [event["id"] for event in rest]
        assert rest[0]["payload"]["title"] == "two"

    def test_the_events_carry_their_identifiers_and_payloads(
        self, client: TestClient
    ) -> None:
        run_id = new_run(client, "readable")
        event = client.get(f"/runs/{run_id}/log").json()[0]

        assert event["id"].startswith("evt_")
        assert event["run_id"] == run_id
        assert event["payload"]["goal"] == "readable"
        assert event["ts"]

    def test_an_unknown_run_is_a_404(self, client: TestClient) -> None:
        assert client.get(f"/runs/{RunId.generate()}/log").status_code == 404

    def test_an_absurd_limit_is_refused(self, client: TestClient) -> None:
        run_id = new_run(client)
        assert client.get(f"/runs/{run_id}/log?limit=0").status_code == 422

    def test_the_log_agrees_with_the_store(
        self, client: TestClient, state_of: object
    ) -> None:
        run_id = new_run(client)
        new_task(client, run_id)

        served = client.get(f"/runs/{run_id}/log").json()
        state = state_of(client)  # type: ignore[operator]
        stored = state.store.events.read_run(RunId(run_id))

        assert [event["id"] for event in served] == [str(event.id) for event in stored]


class TestReplayIsAuthoritative:
    """Reads go through the log, not the materialized tables."""

    def test_the_api_and_a_replay_agree(
        self, client: TestClient, state_of: object
    ) -> None:
        run_id = new_run(client, "consistent")
        new_task(client, run_id, title="one")
        new_task(client, run_id, title="two")

        state = state_of(client)  # type: ignore[operator]
        replayed = state.store.replay(RunId(run_id))
        served = client.get(f"/runs/{run_id}").json()

        assert served["goal"] == replayed.goal
        assert len(served["tasks"]) == len(replayed.tasks)

    def test_the_materialized_view_stays_consistent(
        self, client: TestClient, state_of: object
    ) -> None:
        run_id = new_run(client)
        new_task(client, run_id)
        client.patch(
            f"/runs/{run_id}/tasks/{client.get(f'/runs/{run_id}/tasks').json()[0]['id']}",
            json={"title": "amended"},
        )

        state = state_of(client)  # type: ignore[operator]
        ok, differences = state.store.verify(RunId(run_id))
        assert ok, differences

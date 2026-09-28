"""Provider-shaped handoff contracts, not a claim of live LLM evaluation."""

import json

import pytest
from langchain_core.messages import ToolMessage

from apps.agents.tools.artifact_manager_agent import _summarize_result


@pytest.mark.parametrize(
    ("kind", "datasets", "members", "grain", "need"),
    [
        ("dimension", ["raw_visits"], ["raw_visits.status"], "One visit", "Group statuses"),
        (
            "measure",
            ["raw_visits"],
            ["raw_visits.count"],
            "Visits",
            "Approved share with a defined denominator",
        ),
        (
            "dataset",
            ["raw_forms"],
            ["raw_forms.form_data"],
            "One repeat occurrence per form",
            "Expand repeated questions",
        ),
        (
            "dataset",
            ["raw_messages"],
            ["raw_messages.content"],
            "One reviewed message per snapshot",
            "Classify text",
        ),
        (
            "relationship",
            ["raw_sessions", "raw_participants"],
            ["raw_sessions.participant_identifier", "raw_participants.identifier"],
            "Many sessions to one participant within a tenant",
            "Prove unambiguous participant identity",
        ),
        (
            "relationship",
            ["tenant_a_forms", "tenant_b_cases"],
            [],
            "Unresolved across tenants",
            "Ask whether a legitimate cross-tenant relationship exists",
        ),
    ],
)
def test_provider_shaped_proposals_keep_kind_grain_and_uncertainty(
    kind, datasets, members, grain, need
):
    requirement = {
        "kind": kind,
        "source_datasets": datasets,
        "source_members": members,
        "grain": grain,
        "need": need,
        "decisions": ["User must approve the specific change"],
    }
    result = _summarize_result(
        [], json.dumps({"status": "needs_data_model", "data_requirements": [requirement]})
    )
    assert result["status"] == "needs_data_model"
    assert result["data_requirements"] == [requirement]
    assert result["artifact_id"] is None


def test_runtime_failure_cannot_be_overridden_with_a_model_proposal():
    messages = [
        ToolMessage(
            name="artifact_write",
            tool_call_id="write",
            content=json.dumps(
                {
                    "status": "error",
                    "runtime": {
                        "failures": [
                            {
                                "category": "data_unavailable",
                                "recovery_action": "materialization",
                                "retryable": False,
                            }
                        ]
                    },
                }
            ),
        )
    ]
    result = _summarize_result(
        messages,
        json.dumps(
            {
                "status": "needs_data_model",
                "data_requirements": [
                    {
                        "kind": "dataset",
                        "source_datasets": ["raw_visits"],
                        "source_members": [],
                        "grain": "One visit",
                        "need": "Replace unavailable data",
                        "decisions": [],
                    }
                ],
            }
        ),
    )
    assert result["status"] == "error"
    assert result["runtime_failures"][0]["recovery_action"] == "materialization"
    assert "data_requirements" not in result

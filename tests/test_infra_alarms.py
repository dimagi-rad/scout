"""Guard that the CloudFormation template ships a detection layer (08#7).

infra/scout-stack.yml created log groups but no CloudWatch alarm / SNS /
metric-filter resources, so worker/MCP/process death emitted no operator signal
(arch #257, finding 08#7). These tests pin a minimal detection layer: an SNS
alert topic, metric filters that turn the structured app logs into metrics, and
alarms on infrastructure health (EC2/RDS) plus error rate and worker silence.

Pure YAML parse — no AWS calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_TEMPLATE = Path(__file__).resolve().parent.parent / "infra" / "scout-stack.yml"


class _CfnLoader(yaml.SafeLoader):
    """A SafeLoader that treats CloudFormation ``!Ref`` / ``!GetAtt`` etc. short
    tags as opaque (None) so the template parses without executing them."""


# Render every unknown ``!Tag`` (scalar/sequence/mapping) as None — we only
# assert on resource structure, not the values inside intrinsic functions.
_CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


@pytest.fixture(scope="module")
def template() -> dict:
    with _TEMPLATE.open() as f:
        return yaml.load(f, Loader=_CfnLoader)  # noqa: S506 — dedicated CFN-tag loader, trusted file


def _resources_of_type(template: dict, cfn_type: str) -> dict:
    return {name: r for name, r in template["Resources"].items() if r.get("Type") == cfn_type}


def test_alarm_email_parameter_exists(template):
    assert "AlarmEmail" in template["Parameters"], (
        "an AlarmEmail parameter must exist so alarms can notify an operator"
    )


def test_sns_alert_topic_exists(template):
    topics = _resources_of_type(template, "AWS::SNS::Topic")
    assert topics, "an SNS topic is required to route alarms to operators (08#7)"
    subs = _resources_of_type(template, "AWS::SNS::Subscription")
    has_email_sub = any(
        s.get("Properties", {}).get("Protocol") == "email" for s in subs.values()
    ) or any("Subscription" in t.get("Properties", {}) for t in topics.values())
    assert has_email_sub, "the alert topic must have an email subscription"


def test_metric_filters_exist_for_app_logs(template):
    filters = _resources_of_type(template, "AWS::Logs::MetricFilter")
    assert filters, "metric filters must convert structured app logs into metrics (08#7)"


def test_cloudwatch_alarms_exist(template):
    alarms = _resources_of_type(template, "AWS::CloudWatch::Alarm")
    assert len(alarms) >= 3, (
        f"expected several alarms (EC2/RDS health, error rate, worker silence); found {len(alarms)}"
    )
    # Every alarm must route to the SNS topic.
    for name, alarm in alarms.items():
        props = alarm.get("Properties", {})
        assert props.get("AlarmActions"), f"alarm {name} has no AlarmActions (no notification)"


def test_worker_silence_alarm_treats_missing_data_as_breaching(template):
    """Worker death manifests as the ABSENCE of log events, so the worker-health
    alarm must treat missing data as breaching (otherwise a dead worker that
    emits nothing never alarms)."""
    alarms = _resources_of_type(template, "AWS::CloudWatch::Alarm")
    breaching = [
        n
        for n, a in alarms.items()
        if a.get("Properties", {}).get("TreatMissingData") == "breaching"
    ]
    assert breaching, (
        "at least one alarm must use TreatMissingData: breaching to detect a "
        "silent (dead) worker/process (08#7)"
    )

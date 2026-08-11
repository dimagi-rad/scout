"""Fitness tests for infra/scout-stack.yml network/credential hardening (arch #261).

These parse the CloudFormation template straight off disk (no AWS needed) and
assert the security posture that arch findings 10#8 (IMDSv2) and 10#9 (CI deploy
role scope) hardened, plus that the #257 alarm layer stays wired to its SNS
topic. A future template edit that regresses any of these fails CI instead of
silently shipping.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
STACK_PATH = REPO_ROOT / "infra" / "scout-stack.yml"


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form intrinsics.

    ``!Ref`` / ``!Sub`` / ``!GetAtt`` / ``!Select`` etc. are collapsed to their
    raw node value (a scalar becomes its string, so ``!Ref Foo`` -> ``"Foo"`` and
    ``!Sub 'arn:...'`` -> ``"arn:..."``). That is enough to assert on the plain
    data structure without pulling in a full CloudFormation parser.
    """


def _cfn_intrinsic(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_CfnLoader.add_multi_constructor("!", _cfn_intrinsic)


def _load_stack() -> dict:
    # S506 is suppressed because _CfnLoader subclasses SafeLoader and only adds
    # constructors that build plain data (no object instantiation) — still safe.
    return yaml.load(STACK_PATH.read_text(), Loader=_CfnLoader)  # noqa: S506


def _resources() -> dict:
    return _load_stack()["Resources"]


def test_ec2_requires_imdsv2():
    """A plain-GET SSRF must not be able to read instance-role creds from IMDS.

    IMDSv2 (HttpTokens: required) forces a PUT to obtain a session token before
    any metadata GET succeeds, which a GET-only app-layer SSRF cannot do; the
    hop limit of 1 keeps IMDS off container/bridge networks (arch 10#8).
    """
    props = _resources()["EC2Instance"]["Properties"]
    opts = props.get("MetadataOptions") or {}
    assert opts.get("HttpTokens") == "required", (
        "EC2 instance must set MetadataOptions.HttpTokens: required so IMDSv1 "
        "(token-less GET) can't be used to steal the instance role credentials "
        "via SSRF (arch 10#8)."
    )
    assert opts.get("HttpEndpoint", "enabled") == "enabled"
    assert opts.get("HttpPutResponseHopLimit") == 1


def _deploy_policy_statements() -> list[dict]:
    role = _resources()["GitHubDeployRole"]
    statements: list[dict] = []
    for policy in role["Properties"]["Policies"]:
        statements.extend(policy["PolicyDocument"]["Statement"])
    return statements


def _statements_for_action(action: str) -> list[dict]:
    matched: list[dict] = []
    for stmt in _deploy_policy_statements():
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if action in actions:
            matched.append(stmt)
    return matched


def _statement_resources(stmt: dict) -> list:
    resource = stmt.get("Resource", [])
    return [resource] if isinstance(resource, str) else resource


def test_batch_get_secret_value_stays_unscoped():
    """BatchGetSecretValue must stay on Resource '*' — scoping it breaks the deploy.

    This test asserted the opposite until 0def249, and that assertion was wrong
    in a way that cost two broken deploys. BatchGetSecretValue is an **API-level**
    action and is not resource-scoped: narrowing it to ARNs denies the call
    outright with "no identity-based policy allows the
    secretsmanager:BatchGetSecretValue action", so kamal cannot fetch any secret.
    arch 10#9 tightened it and broke the staging deploy, re-breaking what cd89826
    had already fixed.

    Confinement to the scout namespace lives on **GetSecretValue**, which is
    resource-scoped and is pinned by the tests below. That is the assertion with
    security value; this one exists so the next well-intentioned tightening fails
    the build instead of the deploy.
    """
    statements = _statements_for_action("secretsmanager:BatchGetSecretValue")
    assert statements, "expected a BatchGetSecretValue statement (kamal uses it to fetch secrets)"
    for stmt in statements:
        assert "*" in _statement_resources(stmt), (
            "secretsmanager:BatchGetSecretValue must stay on Resource '*'. It is an "
            "API-level action and cannot be resource-scoped; narrowing it denies the "
            "deploy's secret fetch outright. Scope GetSecretValue instead — see 0def249."
        )


def test_get_secret_value_is_scoped_to_the_scout_namespace():
    """The confinement BatchGetSecretValue cannot provide has to live here.

    Since the batch action is necessarily account-wide, GetSecretValue is the only
    thing between the deploy role and every secret in the account, so it must
    never be widened to '*'.
    """
    statements = _statements_for_action("secretsmanager:GetSecretValue")
    assert statements, "expected a GetSecretValue statement"
    resources = [r for stmt in statements for r in _statement_resources(stmt)]
    assert "*" not in resources, (
        "secretsmanager:GetSecretValue must not be granted on Resource '*' — it is the "
        "per-secret gate that confines the deploy to the scout-namespaced prefixes, and "
        "BatchGetSecretValue is deliberately unscoped, so widening this leaves nothing."
    )
    assert any("secret:SCOUT_" in str(r) for r in resources), (
        "expected GetSecretValue scoped to the SCOUT_ prefix the deploy actually reads"
    )


def test_get_secret_value_not_account_wide_rds():
    """GetSecretValue must not use the account-wide ``rds!*`` prefix.

    ``secret:rds!*`` matches every RDS-managed master-password secret in the
    account, not just scout-db. The deploy only ever reads scout-db's master
    secret, so it should be scoped to that exact ARN (arch 10#9).
    """
    for stmt in _statements_for_action("secretsmanager:GetSecretValue"):
        for resource in _statement_resources(stmt):
            assert not (isinstance(resource, str) and resource.endswith("secret:rds!*")), (
                "GetSecretValue must not use the account-wide 'rds!*' prefix — it grants "
                "read on every RDS master password in the account. Scope it to the scout-db "
                "master secret ARN via !GetAtt RDSInstance.MasterUserSecret.SecretArn (arch 10#9)."
            )


def test_deploy_role_can_still_read_the_rds_master_secret():
    """Guard against over-tightening: the deploy resolves DATABASE_URL from the
    RDS-managed master secret, so the CI role must still be able to read it."""
    resources = [
        resource
        for stmt in _statements_for_action("secretsmanager:GetSecretValue")
        for resource in _statement_resources(stmt)
    ]
    assert any("RDSInstance.MasterUserSecret.SecretArn" in str(r) for r in resources), (
        "The CI deploy role must retain read on the RDS master secret "
        "(!GetAtt RDSInstance.MasterUserSecret.SecretArn) or resolve-database-url.sh "
        "can no longer build DATABASE_URL and the deploy breaks."
    )


def _resources_of_type(cfn_type: str) -> dict:
    return {name: r for name, r in _resources().items() if r.get("Type") == cfn_type}


def test_every_alarm_notifies_the_alert_topic():
    """The #257 detection layer only helps if every alarm reaches an operator.

    A CloudWatch alarm with no AlarmActions pointing at the SNS AlertTopic fires
    silently — exactly the 2026-06-09 blind spot #257 exists to close.
    """
    alarms = _resources_of_type("AWS::CloudWatch::Alarm")
    assert alarms, "expected CloudWatch alarms in the stack (arch #257 detection layer)"
    unwired = [
        name
        for name, alarm in alarms.items()
        if "AlertTopic" not in (alarm["Properties"].get("AlarmActions") or [])
    ]
    assert not unwired, f"CloudWatch alarms not wired to the SNS AlertTopic (arch #257): {unwired}"


def test_every_error_metric_has_an_alarm():
    """Every ``*ErrorCount`` metric filter must have an alarm consuming it.

    An error metric with no alarm is computed but watched by nothing — the cost
    of the metric with none of the signal (arch #257).
    """
    alarmed = {
        alarm["Properties"]["MetricName"]
        for alarm in _resources_of_type("AWS::CloudWatch::Alarm").values()
    }
    error_metrics = {
        transform["MetricName"]
        for mf in _resources_of_type("AWS::Logs::MetricFilter").values()
        for transform in mf["Properties"]["MetricTransformations"]
        if transform["MetricName"].endswith("ErrorCount")
    }
    orphans = sorted(error_metrics - alarmed)
    assert not orphans, f"error metric(s) with no alarm consuming them (arch #257): {orphans}"

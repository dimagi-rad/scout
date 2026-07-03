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

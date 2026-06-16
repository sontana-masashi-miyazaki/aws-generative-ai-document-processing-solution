from typing import Any, Dict

from aws_cdk import aws_ec2
from constructs import Construct

from .config import DeploymentConfig


def resolve_lambda_network(
    scope: Construct, config: DeploymentConfig
) -> Dict[str, Any]:
    if not config.lambda_vpc_id and not config.lambda_vpc_name:
        return {"enabled": False}

    lookup_kwargs = {}
    if config.lambda_vpc_id:
        lookup_kwargs["vpc_id"] = config.lambda_vpc_id
    if config.lambda_vpc_name:
        lookup_kwargs["vpc_name"] = config.lambda_vpc_name

    vpc = aws_ec2.Vpc.from_lookup(scope, "processing-vpc", **lookup_kwargs)
    security_groups = [
        aws_ec2.SecurityGroup.from_security_group_id(
            scope,
            f"imported-lambda-sg-{index}",
            security_group_id=security_group_id,
            mutable=False,
        )
        for index, security_group_id in enumerate(config.lambda_security_group_ids)
    ]

    if config.create_lambda_security_group:
        lambda_security_group = aws_ec2.SecurityGroup(
            scope,
            "processing-lambda-sg",
            vpc=vpc,
            allow_all_outbound=config.lambda_allow_all_outbound,
            description="Scoped egress security group for document processing Lambdas.",
        )
        if not config.lambda_allow_all_outbound:
            lambda_security_group.add_egress_rule(
                aws_ec2.Peer.any_ipv4(),
                aws_ec2.Port.tcp(443),
                "HTTPS egress to AWS services through NAT or VPC endpoints",
            )
            lambda_security_group.add_egress_rule(
                aws_ec2.Peer.ipv4(vpc.vpc_cidr_block),
                aws_ec2.Port.tcp(53),
                "VPC DNS over TCP",
            )
            lambda_security_group.add_egress_rule(
                aws_ec2.Peer.ipv4(vpc.vpc_cidr_block),
                aws_ec2.Port.udp(53),
                "VPC DNS over UDP",
            )
        security_groups.append(lambda_security_group)

    subnet_selection = (
        aws_ec2.SubnetSelection(
            subnets=[
                aws_ec2.Subnet.from_subnet_id(
                    scope,
                    f"imported-lambda-subnet-{index}",
                    subnet_id=subnet_id,
                )
                for index, subnet_id in enumerate(config.lambda_subnet_ids)
            ]
        )
        if config.lambda_subnet_ids
        else aws_ec2.SubnetSelection(subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS)
    )

    return {
        "enabled": True,
        "vpc": vpc,
        "security_groups": security_groups,
        "subnet_selection": subnet_selection,
    }


def lambda_network_kwargs(network: Dict[str, Any]) -> Dict[str, Any]:
    if not network["enabled"]:
        return {}

    kwargs: Dict[str, Any] = {
        "vpc": network["vpc"],
        "vpc_subnets": network["subnet_selection"],
    }
    if network["security_groups"]:
        kwargs["security_groups"] = network["security_groups"]
    return kwargs

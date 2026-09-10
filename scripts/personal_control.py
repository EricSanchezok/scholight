"""Render least-privilege manual control roles for the personal runtime."""

from __future__ import annotations

import json

from personal_runtime import arn, base, ref, resource, statement, sub


def control() -> dict:
    template = base(
        [
            "GitHubOidcProviderArn",
            "GitHubOidcSubjectPrefix",
            "ClusterArn",
            "ClusterName",
            "HostRoleArn",
        ]
    )
    resources, outputs = template["Resources"], template["Outputs"]
    app_roles = [
        sub("arn:aws:iam::${AWS::AccountId}:role/ScholightPersonal" + name + "*")
        for name in ("Api", "Web", "Extract", "Metadata", "Migration")
    ]
    region = {"StringEquals": {"aws:RequestedRegion": ref("AWS::Region")}}
    runtime_stack = sub(
        "arn:aws:cloudformation:${AWS::Region}:${AWS::AccountId}:stack/sanchezcloud-scholight-personal-runtime/*"
    )
    task_family = sub(
        "arn:aws:ecs:${AWS::Region}:${AWS::AccountId}:task-definition/scholight-personal-*"
    )
    cluster_tasks = sub("arn:aws:ecs:${AWS::Region}:${AWS::AccountId}:task/${ClusterName}/*")
    log_group = sub(
        "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/sanchezcloud/scholight/personal/*"
    )
    service_statements = [
        statement(
            [
                "iam:CreateRole",
                "iam:DeleteRole",
                "iam:GetRole",
                "iam:UpdateAssumeRolePolicy",
                "iam:PutRolePolicy",
                "iam:DeleteRolePolicy",
                "iam:GetRolePolicy",
                "iam:ListRolePolicies",
                "iam:TagRole",
                "iam:UntagRole",
            ],
            app_roles,
        ),
        statement(
            ["iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy"], ref("HostRoleArn")
        ),
        statement(
            ["iam:PassRole"],
            app_roles,
            Condition={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
        ),
        statement(
            ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"], "*", Condition=region
        ),
        statement(
            [
                "ecs:DeregisterTaskDefinition",
                "ecs:TagResource",
                "ecs:UntagResource",
                "ecs:ListTagsForResource",
            ],
            [
                task_family,
                sub(
                    "arn:aws:ecs:${AWS::Region}:${AWS::AccountId}:service/${ClusterName}/scholight-personal-*"
                ),
            ],
        ),
        statement(
            ["ecs:CreateService", "ecs:UpdateService", "ecs:DeleteService", "ecs:DescribeServices"],
            sub(
                "arn:aws:ecs:${AWS::Region}:${AWS::AccountId}:service/${ClusterName}/scholight-personal-*"
            ),
        ),
        statement(
            [
                "logs:CreateLogGroup",
                "logs:DeleteLogGroup",
                "logs:PutRetentionPolicy",
                "logs:TagResource",
                "logs:UntagResource",
                "logs:ListTagsForResource",
            ],
            log_group,
        ),
        statement(["logs:DescribeLogGroups"], "*", Condition=region),
        statement(
            [
                "ssm:PutParameter",
                "ssm:GetParameter",
                "ssm:DeleteParameter",
                "ssm:AddTagsToResource",
                "ssm:RemoveTagsFromResource",
                "ssm:ListTagsForResource",
            ],
            sub(
                "arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/sanchezcloud/personal/background/scholight-metadata"
            ),
        ),
    ]
    resources["CloudFormationRole"] = resource(
        "AWS::IAM::Role",
        RoleName="ScholightPersonalCloudFormation",
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "cloudformation.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        },
        Policies=[
            {
                "PolicyName": "OwnProductRuntime",
                "PolicyDocument": {"Version": "2012-10-17", "Statement": service_statements},
            }
        ],
    )
    for name, environments, statements in (
        (
            "Deploy",
            ["personal-infrastructure", "personal-preview"],
            [
                statement(
                    [
                        "cloudformation:CreateChangeSet",
                        "cloudformation:DescribeChangeSet",
                        "cloudformation:ExecuteChangeSet",
                        "cloudformation:DeleteChangeSet",
                        "cloudformation:DescribeStacks",
                        "cloudformation:DescribeStackEvents",
                        "cloudformation:GetTemplate",
                    ],
                    runtime_stack,
                ),
                statement(
                    ["iam:PassRole"],
                    arn("CloudFormationRole"),
                    Condition={
                        "StringEquals": {"iam:PassedToService": "cloudformation.amazonaws.com"}
                    },
                ),
            ],
        ),
        (
            "Database",
            ["personal-database"],
            [
                statement(["cloudformation:DescribeStacks"], runtime_stack),
                statement(
                    ["ecs:RunTask"],
                    sub(
                        "arn:aws:ecs:${AWS::Region}:${AWS::AccountId}:task-definition/scholight-personal-migration:*"
                    ),
                    Condition={"ArnEquals": {"ecs:cluster": ref("ClusterArn")}},
                ),
                statement(["ecs:DescribeTasks", "ecs:StopTask"], cluster_tasks),
                statement(["ecs:DescribeTaskDefinition"], "*", Condition=region),
                statement(
                    ["iam:PassRole"],
                    [
                        sub(
                            "arn:aws:iam::${AWS::AccountId}:role/ScholightPersonalMigrationExecution"
                        ),
                        sub("arn:aws:iam::${AWS::AccountId}:role/ScholightPersonalMigrationTask"),
                    ],
                    Condition={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
                ),
                statement(
                    ["logs:GetLogEvents", "logs:DescribeLogStreams"],
                    sub(
                        "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/sanchezcloud/scholight/personal/migration:*"
                    ),
                ),
            ],
        ),
    ):
        resources[name + "Role"] = resource(
            "AWS::IAM::Role",
            RoleName="ScholightPersonal" + name,
            AssumeRolePolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Federated": ref("GitHubOidcProviderArn")},
                        "Action": "sts:AssumeRoleWithWebIdentity",
                        "Condition": {
                            "StringEquals": {
                                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                                "token.actions.githubusercontent.com:sub": [
                                    sub("${GitHubOidcSubjectPrefix}:environment:" + env)
                                    for env in environments
                                ],
                            }
                        },
                    }
                ],
            },
            Policies=[
                {
                    "PolicyName": "ManualProductControl",
                    "PolicyDocument": {"Version": "2012-10-17", "Statement": statements},
                }
            ],
        )
        outputs[name + "RoleArn"] = {"Value": arn(name + "Role")}
    outputs["CloudFormationRoleArn"] = {"Value": arn("CloudFormationRole")}
    return template


if __name__ == "__main__":
    print(json.dumps(control(), indent=2))

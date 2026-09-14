"""Render product-owned personal ECS infrastructure. No AWS calls or secret values."""

from __future__ import annotations

import argparse
import json
from typing import Any


def ref(name: str) -> dict[str, str]:
    return {"Ref": name}


def sub(value: str) -> dict[str, str]:
    return {"Fn::Sub": value}


def arn(name: str) -> dict[str, list[str]]:
    return {"Fn::GetAtt": [name, "Arn"]}


def resource(kind: str, **properties: Any) -> dict[str, Any]:
    return {"Type": kind, "Properties": properties}


def statement(actions: list[str], resources: Any, **extra: Any) -> dict[str, Any]:
    return {"Effect": "Allow", "Action": actions, "Resource": resources, **extra}


def role(name: str, statements: list[dict[str, Any]]) -> dict[str, Any]:
    return resource(
        "AWS::IAM::Role",
        RoleName="ScholightPersonal" + name,
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        },
        Policies=(
            [
                {
                    "PolicyName": "ProductCapabilities",
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": statements,
                    },
                }
            ]
            if statements
            else []
        ),
    )


def base(parameters: list[str]) -> dict[str, Any]:
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Parameters": {name: {"Type": "String"} for name in ["ExpectedAccountId", *parameters]},
        "Rules": {
            "ExpectedAccount": {
                "Assertions": [
                    {
                        "Assert": {"Fn::Equals": [ref("AWS::AccountId"), ref("ExpectedAccountId")]},
                        "AssertDescription": "Deploy only into the reviewed personal account.",
                    }
                ]
            }
        },
        "Resources": {},
        "Outputs": {},
    }


def foundation() -> dict[str, Any]:
    template = base(["GitHubOidcProviderArn", "GitHubOidcSubjectPrefix"])
    resources, outputs = template["Resources"], template["Outputs"]
    resources["ConfigurationKey"] = resource(
        "AWS::KMS::Key",
        EnableKeyRotation=True,
        KeyPolicy={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": sub("arn:aws:iam::${AWS::AccountId}:root")},
                    "Action": "kms:*",
                    "Resource": "*",
                }
            ],
        },
    )
    resources["ConfigurationKey"]["DeletionPolicy"] = "RetainExceptOnCreate"
    resources["ConfigurationKey"]["UpdateReplacePolicy"] = "Retain"
    outputs["ConfigurationKeyArn"] = {"Value": arn("ConfigurationKey")}
    resources["ReleaseBucket"] = resource(
        "AWS::S3::Bucket",
        BucketName=sub("scholight-personal-releases-${AWS::AccountId}-${AWS::Region}"),
        VersioningConfiguration={"Status": "Enabled"},
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
        BucketEncryption={
            "ServerSideEncryptionConfiguration": [
                {
                    "ServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "aws:kms",
                        "KMSMasterKeyID": arn("ConfigurationKey"),
                    },
                    "BucketKeyEnabled": True,
                }
            ]
        },
    )
    resources["ReleaseBucket"].update(
        DeletionPolicy="RetainExceptOnCreate", UpdateReplacePolicy="Retain"
    )
    resources["ReleaseBucketPolicy"] = resource(
        "AWS::S3::BucketPolicy",
        Bucket=ref("ReleaseBucket"),
        PolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:*",
                    "Resource": [arn("ReleaseBucket"), sub("${ReleaseBucket.Arn}/*")],
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                }
            ],
        },
    )
    outputs["ReleaseBucketName"] = {"Value": ref("ReleaseBucket")}
    for name, path in {
        "Core": "core",
        "McpDelegation": "mcp-delegation",
        "DatabaseRuntime": "database-runtime",
        "DatabaseMigrator": "database-migrator",
        "SearchApi": "search-api",
        "SearchSync": "search-sync",
        "Mail": "mail",
    }.items():
        logical = name + "Secret"
        resources[logical] = resource(
            "AWS::SecretsManager::Secret",
            Name="/sanchezcloud/scholight/personal/" + path,
            KmsKeyId=arn("ConfigurationKey"),
            Description="Provision securely before enabling personal Scholight tasks",
        )
        resources[logical].update(
            DeletionPolicy="RetainExceptOnCreate", UpdateReplacePolicy="Retain"
        )
        outputs[logical + "Arn"] = {"Value": ref(logical)}
    for name in ("Api", "Web", "Extract", "Metadata", "Ingest"):
        logical = name + "Repository"
        resources[logical] = resource(
            "AWS::ECR::Repository",
            RepositoryName="scholight-personal-" + name.lower(),
            ImageTagMutability="IMMUTABLE",
            ImageScanningConfiguration={"ScanOnPush": True},
            LifecyclePolicy={
                "LifecyclePolicyText": json.dumps(
                    {
                        "rules": [
                            {
                                "rulePriority": 1,
                                "selection": {
                                    "tagStatus": "untagged",
                                    "countType": "sinceImagePushed",
                                    "countUnit": "days",
                                    "countNumber": 7,
                                },
                                "action": {"type": "expire"},
                            }
                        ]
                    }
                )
            },
        )
        resources[logical].update(
            DeletionPolicy="RetainExceptOnCreate", UpdateReplacePolicy="Retain"
        )
        outputs[logical + "Name"] = {"Value": ref(logical)}
    resources["PublishRole"] = resource(
        "AWS::IAM::Role",
        RoleName="ScholightPersonalImagePublisher",
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
                            "token.actions.githubusercontent.com:sub": sub(
                                "${GitHubOidcSubjectPrefix}:environment:personal-image-publish"
                            ),
                        }
                    },
                }
            ],
        },
        Policies=[
            {
                "PolicyName": "PublishImmutableImages",
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        statement(["ecr:GetAuthorizationToken"], "*"),
                        statement(
                            ["s3:PutObject", "s3:GetObject"], sub("${ReleaseBucket.Arn}/releases/*")
                        ),
                        statement(
                            ["kms:GenerateDataKey", "kms:Decrypt"],
                            arn("ConfigurationKey"),
                            Condition={
                                "StringEquals": {
                                    "kms:ViaService": sub("s3.${AWS::Region}.amazonaws.com")
                                }
                            },
                        ),
                        statement(
                            [
                                "ecr:BatchCheckLayerAvailability",
                                "ecr:InitiateLayerUpload",
                                "ecr:UploadLayerPart",
                                "ecr:CompleteLayerUpload",
                                "ecr:PutImage",
                                "ecr:BatchGetImage",
                                "ecr:GetDownloadUrlForLayer",
                                "ecr:DescribeImages",
                                "ecr:DescribeImageScanFindings",
                            ],
                            [
                                arn(name + "Repository")
                                for name in ("Api", "Web", "Extract", "Metadata", "Ingest")
                            ],
                        ),
                    ],
                },
            }
        ],
    )
    outputs["PublishRoleArn"] = {"Value": arn("PublishRole")}
    return template


def secret(environment: str, parameter: str, field: str) -> dict[str, Any]:
    return {"Name": environment, "ValueFrom": sub("${" + parameter + "}:" + field + "::")}


def runtime() -> dict[str, Any]:
    template = base(
        [
            "ClusterArn",
            "DomainName",
            "PreviewDomainName",
            "HostPrivateAddress",
            "DatabaseCaPem",
            "ConfigurationKeyArn",
            "AvatarBucketName",
            "AvatarKeyArn",
            "AvatarRegion",
            "CoreSecretArn",
            "McpDelegationSecretArn",
            "DatabaseRuntimeSecretArn",
            "DatabaseMigratorSecretArn",
            "SearchApiSecretArn",
            "SearchSyncSecretArn",
            "MailSecretArn",
            "ApiImage",
            "WebImage",
            "ExtractImage",
            "MetadataImage",
        ]
    )
    template["Parameters"]["ApplicationEnabled"] = {
        "Type": "String",
        "Default": "false",
        "AllowedValues": ["false", "true"],
    }
    template["Parameters"]["HostRoleName"] = {"Type": "String", "Default": ""}
    template["Parameters"]["MetadataEnabled"] = {
        "Type": "String",
        "Default": "false",
        "AllowedValues": ["false", "true"],
    }
    template["Parameters"]["MetadataBatchSize"] = {
        "Type": "Number",
        "Default": 64,
        "MinValue": 1,
        "MaxValue": 512,
    }
    for name in ("Api", "Web", "Extract", "Metadata"):
        template["Parameters"][name + "Image"]["AllowedPattern"] = (
            r"^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/scholight-personal-[a-z]+@sha256:[0-9a-f]{64}$"
        )
    template["Conditions"] = {"Enabled": {"Fn::Equals": [ref("ApplicationEnabled"), "true"]}}
    template["Conditions"]["RegisterBackground"] = {
        "Fn::Not": [{"Fn::Equals": [ref("HostRoleName"), ""]}]
    }
    resources, outputs = template["Resources"], template["Outputs"]
    database = [
        secret("SCHOLIGHT_PG_" + env, "DatabaseRuntimeSecretArn", field)
        for env, field in (
            ("HOST", "host"),
            ("PORT", "port"),
            ("DATABASE", "dbname"),
            ("USER", "username"),
            ("PASSWORD", "password"),
        )
    ]
    common = {
        "SCHOLIGHT_RUNTIME_PROFILE": "lean",
        "SCHOLIGHT_DISABLE_DOTENV": "1",
        "SCHOLIGHT_DATA_ROOT": "/tmp/scholight",  # nosec B108
        "AWS_REGION": ref("AWS::Region"),
        "SCHOLIGHT_PG_SSL_ROOT_CERT_PEM": ref("DatabaseCaPem"),
        "SCHOLIGHT_PG_POOL_MIN_SIZE": "1",
        "SCHOLIGHT_PG_POOL_MAX_SIZE": "3",
    }
    for name, memory, reservation, cpu, host_port in (
        ("Api", 768, 384, 128, 18200),
        ("Web", 128, 64, 32, 13200),
        ("Extract", 768, 256, 128, 18201),
        ("Metadata", 768, 768, 256, None),
        ("Migration", 512, 256, 128, None),
    ):
        environment = dict(common)
        secrets: list[dict[str, Any]] = []
        role_statements: list[dict[str, Any]] = []
        image_name = "Api" if name == "Migration" else name
        if name == "Migration":
            secrets.extend(
                {
                    "Name": item["Name"],
                    "ValueFrom": sub(
                        item["ValueFrom"]["Fn::Sub"].replace(
                            "DatabaseRuntimeSecretArn", "DatabaseMigratorSecretArn"
                        )
                    ),
                }
                for item in database
            )
            environment["SCHOLIGHT_MIGRATIONS_DIR"] = "/app/migrations"
        if name in ("Api", "Metadata"):
            secrets.extend(database)
            provider = "SearchApiSecretArn" if name == "Api" else "SearchSyncSecretArn"
            secrets.extend(
                secret("SCHOLIGHT_" + field.upper(), provider, field)
                for field in (
                    "zilliz_uri",
                    "zilliz_token",
                    "embedding_base_url",
                    "embedding_api_key",
                    "embedding_model",
                )
            )
        if name == "Api":
            environment.update(
                {
                    "SCHOLIGHT_SERVER_HOST": "0.0.0.0",  # nosec B104
                    "SCHOLIGHT_SERVER_PORT": "8000",
                    "SCHOLIGHT_PROXY_HEADERS": "true",
                    "SCHOLIGHT_FORWARDED_ALLOW_IPS": ref("HostPrivateAddress"),
                    "SCHOLIGHT_PUBLIC_WEB_URL": sub("https://${DomainName}"),
                    "SCHOLIGHT_CORS_ALLOW_ORIGINS": sub(
                        '["https://${DomainName}","https://${PreviewDomainName}"]'
                    ),
                    "SCHOLIGHT_AUTH_REFRESH_COOKIE_SECURE": "true",
                    "SCHOLIGHT_EXTRACT_ENABLED": "true",
                    "SCHOLIGHT_EXTRACT_SERVICE_URL": sub("http://${HostPrivateAddress}:18201"),
                    "SCHOLIGHT_SURVEY_RUNTIME_ENABLED": "false",
                    "SCHOLIGHT_SURVEY_PUBLIC_MODE": "off",
                    "SCHOLIGHT_AVATAR_S3_BUCKET": ref("AvatarBucketName"),
                    "SCHOLIGHT_AVATAR_S3_REGION": ref("AvatarRegion"),
                    "SCHOLIGHT_AVATAR_S3_ENDPOINT_URL": sub(
                        "https://s3.${AvatarRegion}.amazonaws.com"
                    ),
                    "SCHOLIGHT_SERVER_LIMIT_CONCURRENCY": "32",
                }
            )
            for field in (
                "auth_jwt_secret",
                "anonymous_quota_hmac_secret",
                "access_key_hmac_secret",
                "extract_internal_token",
            ):
                secrets.append(secret("SCHOLIGHT_" + field.upper(), "CoreSecretArn", field))
            secrets.append(
                secret(
                    "SCHOLIGHT_MCP_DELEGATION_JWT_SECRET",
                    "McpDelegationSecretArn",
                    "mcp_delegation_jwt_secret",
                )
            )
            for field in ("access_key_id", "access_key_secret", "account_name"):
                secrets.append(
                    secret(
                        "SCHOLIGHT_ALIYUN_DM_" + field.upper(), "MailSecretArn", "aliyun_" + field
                    )
                )
            role_statements.extend(
                [
                    statement(
                        ["s3:GetObject"],
                        sub("arn:aws:s3:::${AvatarBucketName}/auth/avatars/v1/*"),
                    ),
                    statement(
                        ["kms:Decrypt"],
                        ref("AvatarKeyArn"),
                        Condition={
                            "StringEquals": {
                                "kms:ViaService": sub("s3.${AvatarRegion}.amazonaws.com")
                            }
                        },
                    ),
                ]
            )
        elif name == "Extract":
            environment.update(
                {
                    "SCHOLIGHT_EXTRACT_SERVER_HOST": "0.0.0.0",  # nosec B104
                    "SCHOLIGHT_EXTRACT_SERVER_PORT": "8001",
                    "SCHOLIGHT_EXTRACT_BROWSER_CONCURRENCY": "1",
                    "SCHOLIGHT_EXTRACT_STATIC_CONCURRENCY": "2",
                    "SCHOLIGHT_EXTRACT_CACHE_MAX_BYTES": "33554432",
                }
            )
            secrets.append(
                secret(
                    "SCHOLIGHT_EXTRACT_INTERNAL_TOKEN", "CoreSecretArn", "extract_internal_token"
                )
            )
        elif name == "Metadata":
            environment.update(
                {
                    "SCHOLIGHT_PG_POOL_MAX_SIZE": "2",
                    "SCHOLIGHT_METADATA_SYNC_BATCH_SIZE": ref("MetadataBatchSize"),
                    "SCHOLIGHT_EMBEDDING_CONCURRENCY": "1",
                    "SCHOLIGHT_METADATA_SYNC_TIMEOUT_SECONDS": "6600",
                }
            )
        elif name == "Web":
            environment = {
                "SCHOLIGHT_PUBLIC_WEB_URL": sub("https://${DomainName}"),
                "SCHOLIGHT_API_UPSTREAM": sub("${HostPrivateAddress}:18200"),
            }
        resources[name + "Logs"] = resource(
            "AWS::Logs::LogGroup",
            LogGroupName="/sanchezcloud/scholight/personal/" + name.lower(),
            RetentionInDays=7,
        )
        secret_parameters = sorted(
            {s["ValueFrom"]["Fn::Sub"].split("}", 1)[0][2:] for s in secrets}
        )
        resources[name + "ExecutionRole"] = role(
            name + "Execution",
            [
                statement(["ecr:GetAuthorizationToken"], "*"),
                statement(
                    [
                        "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchCheckLayerAvailability",
                    ],
                    sub(
                        "arn:aws:ecr:${AWS::Region}:${AWS::AccountId}:repository/scholight-personal-"
                        + image_name.lower()
                    ),
                ),
                statement(["logs:CreateLogStream", "logs:PutLogEvents"], arn(name + "Logs")),
                *(
                    [
                        statement(
                            ["secretsmanager:GetSecretValue"], [ref(p) for p in secret_parameters]
                        ),
                        statement(
                            ["kms:Decrypt"],
                            ref("ConfigurationKeyArn"),
                            Condition={
                                "StringEquals": {
                                    "kms:ViaService": sub(
                                        "secretsmanager.${AWS::Region}.amazonaws.com"
                                    )
                                }
                            },
                        ),
                    ]
                    if secrets
                    else []
                ),
            ],
        )
        resources[name + "Role"] = role(name + "Task", role_statements)
        container: dict[str, Any] = {
            "Name": name.lower(),
            "Image": ref(image_name + "Image"),
            "Essential": True,
            "Cpu": cpu,
            "Memory": memory,
            "MemoryReservation": reservation,
            "StopTimeout": 120,
            "LinuxParameters": {"InitProcessEnabled": True},
            "Environment": [{"Name": k, "Value": v} for k, v in environment.items()],
            "LogConfiguration": {
                "LogDriver": "awslogs",
                "Options": {
                    "awslogs-group": ref(name + "Logs"),
                    "awslogs-region": ref("AWS::Region"),
                    "awslogs-stream-prefix": name.lower(),
                    "mode": "non-blocking",
                    "max-buffer-size": "1m",
                },
            },
        }
        if secrets:
            container["Secrets"] = secrets
        if host_port:
            port = {"Api": 8000, "Web": 8080, "Extract": 8001}[name]
            container["PortMappings"] = [
                {"ContainerPort": port, "HostPort": host_port, "Protocol": "tcp"}
            ]
            probe = ({"Api": "/livez", "Extract": "/livez", "Web": "/"})[name]
            command = (
                ["CMD-SHELL", f"wget -q -O /dev/null http://127.0.0.1:{port}{probe}"]
                if name == "Web"
                else [
                    "CMD",
                    "python",
                    "-c",
                    f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}{probe}', timeout=3).close()",
                ]
            )
            container["HealthCheck"] = {
                "Command": command,
                "Interval": 30,
                "Timeout": 5,
                "Retries": 3,
                "StartPeriod": 90,
            }
        else:
            container["Command"] = (
                ["scholight", "store", "migrate"]
                if name == "Migration"
                else ["scholight", "scheduler", "sync"]
            )
        properties: dict[str, Any] = {
            "Family": "scholight-personal-" + name.lower(),
            "RequiresCompatibilities": ["EC2"],
            "NetworkMode": "bridge",
            "RuntimePlatform": {"CpuArchitecture": "ARM64", "OperatingSystemFamily": "LINUX"},
            "ExecutionRoleArn": arn(name + "ExecutionRole"),
            "TaskRoleArn": arn(name + "Role"),
            "ContainerDefinitions": [container],
        }
        if name in ("Metadata", "Migration"):
            properties.update(Cpu="512" if name == "Metadata" else "256", Memory=str(memory))
        resources[name + "Task"] = resource("AWS::ECS::TaskDefinition", **properties)
        outputs[name + "TaskDefinitionArn"] = {"Value": ref(name + "Task")}
        outputs[name + "ExecutionRoleArn"] = {"Value": arn(name + "ExecutionRole")}
        outputs[name + "TaskRoleArn"] = {"Value": arn(name + "Role")}
        if host_port:
            resources[name + "Service"] = resource(
                "AWS::ECS::Service",
                ServiceName="scholight-personal-" + name.lower(),
                Cluster=ref("ClusterArn"),
                TaskDefinition=ref(name + "Task"),
                LaunchType="EC2",
                DesiredCount={"Fn::If": ["Enabled", 1, 0]},
                DeploymentConfiguration={
                    "MinimumHealthyPercent": 0,
                    "MaximumPercent": 100,
                    "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True},
                },
            )
    resources["AdmissionGrant"] = resource(
        "AWS::IAM::Policy",
        PolicyName="ScholightMetadataAdmission",
        Roles=[ref("HostRoleName")],
        PolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                statement(
                    ["ecs:RunTask"],
                    ref("MetadataTask"),
                    Condition={"ArnEquals": {"ecs:cluster": ref("ClusterArn")}},
                ),
                statement(
                    ["iam:PassRole"],
                    [arn("MetadataExecutionRole"), arn("MetadataRole")],
                    Condition={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
                ),
            ],
        },
    )
    resources["AdmissionGrant"]["Condition"] = "RegisterBackground"
    resources["AdmissionRegistration"] = resource(
        "AWS::SSM::Parameter",
        Name="/sanchezcloud/personal/background/scholight-metadata",
        Type="String",
        Value=sub(
            '{"version":1,"name":"scholight-metadata","enabled":${MetadataEnabled},"task_definition":"${MetadataTask}","memory_mib":768,"priority":1,"daily_hour_utc":8}'
        ),
    )
    resources["AdmissionRegistration"].update(
        Condition="RegisterBackground", DependsOn="AdmissionGrant"
    )
    return template


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["foundation", "runtime"])
    args = parser.parse_args()
    print(json.dumps(foundation() if args.stage == "foundation" else runtime(), indent=2))

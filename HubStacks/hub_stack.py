#!/usr/bin/env python3
import yaml

from aws_cdk import (
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elb,
    aws_certificatemanager as acm,
    aws_cognito as cognito,
    custom_resources as cr,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_iam as iam,
    aws_logs as logs,
    aws_ecr as ecr,
    App, Stack
)


class HubStack(Stack):
    def __init__(
        self, app: App, id: str,
        vpc, cognito_user_pool, load_balancer, file_system,
        efs_security_group, ecs_service_security_group, **kwargs
    ) -> None:
        super().__init__(app, id, **kwargs)

        # General configuration variables
        config_yaml = yaml.load(
            open('config.yaml'), Loader=yaml.FullLoader)
        base_name = config_yaml["base_name"]
        domain_prefix = config_yaml['domain_prefix']
        application_prefix = 'pluto-' + domain_prefix
        certificate_arn = config_yaml['certificate_arn']
        container_image_repository_arn = \
            config_yaml['container_image_repository_arn']
        container_image_tag = config_yaml['container_image_tag']
        hosted_zone_name = config_yaml['hosted_zone_name']

        suffix_txt = "secure"
        suffix = f'{suffix_txt}'.lower()
        domain_name = application_prefix + '.' + hosted_zone_name

        cognito_app_client = cognito.UserPoolClient(
            self,
            f'{base_name}UserPoolClient',
            user_pool=cognito_user_pool,
            generate_secret=True,
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO],
            prevent_user_existence_errors=True,
            o_auth=cognito.OAuthSettings(
                callback_urls=[
                    'https://' + domain_name +
                    '/hub/oauth_callback'
                ],
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                    implicit_code_grant=True
                ),
                scopes=[cognito.OAuthScope.PROFILE, cognito.OAuthScope.OPENID]
            )
        )

        describe_cognito_user_pool_client = cr.AwsCustomResource(
            self,
            f'{base_name}UserPoolClientIDResource',
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE),
            on_create=cr.AwsSdkCall(
                service='CognitoIdentityServiceProvider',
                action='describeUserPoolClient',
                parameters={
                    'UserPoolId': cognito_user_pool.user_pool_id,
                    'ClientId': cognito_app_client.user_pool_client_id
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    cognito_app_client.user_pool_client_id)
            )
        )

        cognito_user_pool_client_secret = \
            describe_cognito_user_pool_client.get_response_field(
                'UserPoolClient.ClientSecret'
            )

        cognito_user_pool_domain = cognito.UserPoolDomain(
            self,
            f'{base_name}UserPoolDomain',
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=application_prefix + '-' + suffix
            ),
            user_pool=cognito_user_pool
        )

        # ECS task roles and definition
        ecs_task_execution_role = iam.Role(
            self, f'{base_name}TaskExecutionRole',
            assumed_by=iam.ServicePrincipal('ecs-tasks.amazonaws.com')
        )

        efs_mount_point = ecs.MountPoint(
            container_path='/home',
            source_volume='efs-volume',
            read_only=False
        )

        managed_policy_arn = 'arn:aws:iam::aws:policy/service-role/' \
            'AmazonECSTaskExecutionRolePolicy'
        ecs_task_execution_role.add_managed_policy(
            iam.ManagedPolicy.from_managed_policy_arn(
                self,
                f'{base_name}ServiceRole',
                managed_policy_arn=managed_policy_arn
            )
        )

        ecs_task_role = iam.Role(
            self,
            f'{base_name}TaskRole',
            assumed_by=iam.ServicePrincipal('ecs-tasks.amazonaws.com')
        )

        ecs_task_role.add_to_policy(
            iam.PolicyStatement(
                resources=['*'],
                actions=['cloudwatch:PutMetricData', 'cloudwatch:ListMetrics']
            )
        )

        ecs_task_role.add_to_policy(
            iam.PolicyStatement(
                resources=['*'],
                actions=[
                    'logs:CreateLogStream',
                    'logs:DescribeLogGroups',
                    'logs:DescribeLogStreams',
                    'logs:CreateLogGroup',
                    'logs:PutLogEvents',
                    'logs:PutRetentionPolicy'
                ]
            )
        )

        ecs_task_role.add_to_policy(
            iam.PolicyStatement(
                resources=['*'],
                actions=['ec2:DescribeRegions']
            )
        )

        ecs_task_definition = ecs.FargateTaskDefinition(
            self,
            f'{base_name}TaskDefinition',
            cpu=512,
            memory_limit_mib=1024,
            execution_role=ecs_task_execution_role,
            task_role=ecs_task_role
        )

        # ECS Container definition, service, target group and ALB attachment
        repository = ecr.Repository.from_repository_arn(
            self, "Repo", container_image_repository_arn
        )

        ecs_container = ecs_task_definition.add_container(
            f'{base_name}Container',
            image=ecs.ContainerImage.from_ecr_repository(
                repository=repository,
                tag=container_image_tag
            ),
            privileged=False,
            port_mappings=[
                ecs.PortMapping(
                    container_port=8000,
                    host_port=8000,
                    protocol=ecs.Protocol.TCP
                )
            ],
            logging=ecs.LogDriver.aws_logs(
                stream_prefix=f'{base_name}ContainerLogs-',
                log_retention=logs.RetentionDays.ONE_WEEK
            ),
            environment={
                'OAUTH_CALLBACK_URL':
                    'https://' + domain_name +
                    '/hub/oauth_callback',
                'OAUTH_CLIENT_ID': cognito_app_client.user_pool_client_id,
                'OAUTH_CLIENT_SECRET': cognito_user_pool_client_secret,
                'OAUTH_LOGIN_SERVICE_NAME':
                    config_yaml['oauth_login_service_name'],
                'OAUTH_LOGIN_USERNAME_KEY':
                    config_yaml['oauth_login_username_key'],
                'OAUTH_AUTHORIZE_URL':
                    'https://' + cognito_user_pool_domain.domain_name +
                    '.auth.' + self.region +
                    '.amazoncognito.com/oauth2/authorize',
                'OAUTH_TOKEN_URL':
                    'https://' + cognito_user_pool_domain.domain_name +
                    '.auth.' + self.region + '.amazoncognito.com/oauth2/token',
                'OAUTH_USERDATA_URL':
                    'https://' + cognito_user_pool_domain.domain_name +
                    '.auth.' + self.region +
                    '.amazoncognito.com/oauth2/userInfo',
                'OAUTH_SCOPE': ','.join(config_yaml['oauth_scope'])
            }
        )

        # ECS cluster
        ecs_cluster = ecs.Cluster(
            self, f'{base_name}Cluster',
            vpc=vpc
        )

        ecs_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self, f'{base_name}Service',
            cluster=ecs_cluster,
            task_definition=ecs_task_definition,
            load_balancer=load_balancer,
            desired_count=config_yaml['num_containers'],
            security_groups=[ecs_service_security_group],
            open_listener=False
        )

        ecs_service.target_group.configure_health_check(
            path='/hub',
            enabled=True,
            healthy_http_codes='200-302'
        )

        certificate = acm.Certificate.from_certificate_arn(
            self, "Certificate", certificate_arn
        )
        load_balancer.add_listener(
            f'{base_name}ServiceELBListener',
            port=443,
            protocol=elb.ApplicationProtocol.HTTPS,
            certificates=[certificate],
            default_action=elb.ListenerAction.forward(
                target_groups=[ecs_service.target_group])
        )

        # Cognito admin and initial users from files
        all_users = set()
        for users in ['hub_docker/admins', 'hub_docker/initial_users']:
            try:
                with open(users) as fp:
                    lines = fp.readlines()
                    for line in lines:
                        all_users.add(line.strip())
            except IOError:
                pass
        user_index = 0
        for user in all_users:
            user_index += 1
            cr.AwsCustomResource(
                self,
                f'{base_name}UserPoolUser'+str(user_index),
                policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                    resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE),
                on_create=cr.AwsSdkCall(
                    service='CognitoIdentityServiceProvider',
                    action='adminCreateUser',
                    parameters={
                        'UserPoolId': cognito_user_pool.user_pool_id,
                        'Username': user,
                        'TemporaryPassword': config_yaml[
                            'admin_temp_password'
                        ]
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        cognito_user_pool.user_pool_id)
                )
            )

        efs_security_group.connections.allow_from(
            ecs_service_security_group,
            port_range=ec2.Port.tcp(2049),
            description='Allow EFS from ECS Service containers'
        )

        ecs_task_definition.add_volume(
            name='efs-volume',
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=file_system.file_system_id
            )
        )

        ecs_container.add_mount_points(efs_mount_point)

import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import { AccessLogFormat } from 'aws-cdk-lib/aws-apigateway';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpJwtAuthorizer } from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { DataStack } from './data-stack';
import { PyFunction } from './py-function';
import { SchedulerStack } from './scheduler-stack';

export interface PortalStackProps extends StackProps {
  data: DataStack;
  scheduler: SchedulerStack;
  agentRuntimeArn: string;
  whatsappPhoneNumberId: string;
  legacyTable: string;
  approvedTemplates: string;
  loginTemplate: string;
  alertFrom: string;
  alertTo: string;
  staffCallbackUrls: string[];
  corsOrigins: string[];
  cognitoDomainPrefix: string;
  /** Optional OpenTelemetry: OTLP/HTTP endpoint and the ADOT (or custom) Lambda layer that carries the SDK. */
  otlpEndpoint?: string;
  otelLayerArn?: string;
}

/**
 * portal: two Cognito user pools (staff, participants), one HTTP API with a JWT authorizer per app client,
 * and the staff / participant API Lambdas. Staff are their own admins: the staff API also manages staff accounts
 * (Cognito on the staff pool), so there is no admin group, app client or API.
 */
export class PortalStack extends Stack {
  constructor(scope: Construct, id: string, props: PortalStackProps) {
    super(scope, id, props);
    const { data } = props;
    const whatsappSend = new iam.PolicyStatement({
      actions: ['social-messaging:SendWhatsAppMessage'],
      resources: [`arn:aws:social-messaging:${this.region}:${this.account}:phone-number-id/*`],
    });

    // ---------------------------------------------------------------- staff pool: MFA, code + PKCE
    const preToken = new PyFunction(this, 'StaffPreTokenFn', {
      handler: 'auth_triggers.staff_pre_token.handler',
      data,
    });
    const staffPool = new cognito.UserPool(this, 'StaffPool', {
      userPoolName: 'trialtalk-staff',
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      mfa: cognito.Mfa.REQUIRED,
      mfaSecondFactor: { otp: true, sms: false },
      passwordPolicy: { minLength: 12, requireSymbols: true, tempPasswordValidity: Duration.days(3) },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      customAttributes: {
        site_ids: new cognito.StringAttribute({ mutable: true, maxLen: 256 }), // 'RIVERSIDE,LAKESIDE'
        study_ids: new cognito.StringAttribute({ mutable: true, maxLen: 256 }),
      },
      lambdaTriggers: { preTokenGeneration: preToken },
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    for (const group of ['coordinator', 'PI']) {
      new cognito.CfnUserPoolGroup(this, `Group-${group}`, { userPoolId: staffPool.userPoolId, groupName: group });
    }
    staffPool.addDomain('StaffDomain', { cognitoDomain: { domainPrefix: props.cognitoDomainPrefix } });
    const staffClient = staffPool.addClient('StaffWebClient', {
      generateSecret: false, // SPA: authorization code + PKCE
      authFlows: { userSrp: true },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
        callbackUrls: props.staffCallbackUrls,
        logoutUrls: props.staffCallbackUrls,
      },
      // Staff cannot edit their own role, sites or studies.
      writeAttributes: new cognito.ClientAttributes().withStandardAttributes({ fullname: true }),
      idTokenValidity: Duration.minutes(60),
      accessTokenValidity: Duration.minutes(60),
      refreshTokenValidity: Duration.hours(12),
      preventUserExistenceErrors: true,
    });
    // ---------------------------------------------------------------- participant pool: code on WhatsApp
    const authEnv = { WHATSAPP_PHONE_NUMBER_ID: props.whatsappPhoneNumberId, LOGIN_TEMPLATE: props.loginTemplate };
    const defineFn = new PyFunction(this, 'DefineAuthFn', {
      handler: 'auth_triggers.participant_auth.define_challenge', data,
    });
    const createFn = new PyFunction(this, 'CreateAuthFn', {
      handler: 'auth_triggers.participant_auth.create_challenge', data, environment: authEnv,
    });
    createFn.addToRolePolicy(whatsappSend);
    const verifyFn = new PyFunction(this, 'VerifyAuthFn', {
      handler: 'auth_triggers.participant_auth.verify_challenge', data,
    });
    const participantPool = new cognito.UserPool(this, 'ParticipantPool', {
      userPoolName: 'trialtalk-participants',
      selfSignUpEnabled: false, // accounts are created by staff at enrollment
      signInAliases: { username: true, phone: true },
      mfa: cognito.Mfa.OFF,
      accountRecovery: cognito.AccountRecovery.NONE,
      customAttributes: { participant_id: new cognito.StringAttribute({ mutable: false, maxLen: 32 }) },
      lambdaTriggers: {
        defineAuthChallenge: defineFn,
        createAuthChallenge: createFn,
        verifyAuthChallengeResponse: verifyFn,
      },
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const participantClient = participantPool.addClient('ParticipantWebClient', {
      generateSecret: false,
      authFlows: { custom: true },
      writeAttributes: new cognito.ClientAttributes(),
      readAttributes: new cognito.ClientAttributes()
        .withStandardAttributes({ phoneNumber: true })
        .withCustomAttributes('participant_id'),
      idTokenValidity: Duration.minutes(60),
      accessTokenValidity: Duration.minutes(60),
      refreshTokenValidity: Duration.days(1),
      preventUserExistenceErrors: true,
    });

    // ---------------------------------------------------------------- API Lambdas
    const otelEnv: Record<string, string> = props.otlpEndpoint
      ? { OTEL_EXPORTER_OTLP_ENDPOINT: props.otlpEndpoint, DEPLOY_ENV: this.stackName }
      : {};
    const otelLayers = props.otelLayerArn
      ? [lambda.LayerVersion.fromLayerVersionArn(this, 'OtelLayer', props.otelLayerArn)]
      : [];
    const staffFn = new PyFunction(this, 'StaffApiFn', {
      handler: 'staff_api.handler.handler',
      data,
      login: 'staff_api',
      layers: otelLayers,
      timeout: Duration.seconds(29),
      environment: {
        PARTICIPANT_POOL_ID: participantPool.userPoolId,
        STAFF_POOL_ID: staffPool.userPoolId,
        ...otelEnv,
        REMINDER_FUNCTION_ARN: props.scheduler.reminderFn.functionArn,
        SCHEDULE_GROUP: props.scheduler.groupName,
        SCHEDULER_ROLE_ARN: props.scheduler.schedulerRole.roleArn,
        APPROVED_TEMPLATES: props.approvedTemplates,
        LEGACY_TABLE: props.legacyTable,
      },
    });
    staffFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['scheduler:CreateSchedule', 'scheduler:UpdateSchedule', 'scheduler:DeleteSchedule',
          'scheduler:GetSchedule'],
        resources: [`arn:aws:scheduler:${this.region}:${this.account}:schedule/${props.scheduler.groupName}/rem-*`],
      }),
    );
    staffFn.addToRolePolicy(
      new iam.PolicyStatement({ actions: ['iam:PassRole'], resources: [props.scheduler.schedulerRole.roleArn] }),
    );
    props.scheduler.reminderFn.grantInvoke(staffFn);
    participantPool.grant(staffFn, 'cognito-idp:AdminCreateUser', 'cognito-idp:AdminSetUserPassword',
      'cognito-idp:AdminUpdateUserAttributes', 'cognito-idp:AdminDisableUser');
    if (props.legacyTable) {
      dynamodb.Table.fromTableName(this, 'LegacyTable', props.legacyTable).grant(staffFn,
        'dynamodb:PutItem', 'dynamodb:DeleteItem');
    }

    const participantFn = new PyFunction(this, 'ParticipantApiFn', {
      handler: 'participant_api.handler.handler',
      data,
      login: 'participant_api',
      timeout: Duration.seconds(29), // HTTP API integration limit; the agent usually answers in a few seconds
      memorySize: 512,
      layers: otelLayers,
      environment: {
        ...otelEnv,
        AGENT_RUNTIME_ARN: props.agentRuntimeArn,
        ALERT_FROM: props.alertFrom,
        ALERT_TO: props.alertTo,
      },
    });
    participantFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeAgentRuntime'],
        resources: [props.agentRuntimeArn, `${props.agentRuntimeArn}/*`],
      }),
    );
    if (props.alertFrom) {
      participantFn.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ['ses:SendEmail'],
          resources: [`arn:aws:ses:${this.region}:${this.account}:identity/*`],
        }),
      );
    }

    // Staff account management (/staff/accounts): Cognito on the staff pool. Staff are their own admins.
    staffPool.grant(staffFn, 'cognito-idp:ListUsers', 'cognito-idp:AdminGetUser', 'cognito-idp:AdminCreateUser',
      'cognito-idp:AdminUpdateUserAttributes', 'cognito-idp:AdminDisableUser', 'cognito-idp:AdminEnableUser',
      'cognito-idp:AdminUserGlobalSignOut', 'cognito-idp:AdminResetUserPassword',
      'cognito-idp:AdminListGroupsForUser', 'cognito-idp:AdminAddUserToGroup', 'cognito-idp:AdminRemoveUserFromGroup');

    // ---------------------------------------------------------------- HTTP API
    const issuer = (pool: cognito.UserPool) => `https://cognito-idp.${this.region}.amazonaws.com/${pool.userPoolId}`;
    // ID tokens: aud = app client id, and they carry custom:participant_id / role / site_ids.
    const staffAuth = new HttpJwtAuthorizer('StaffJwt', issuer(staffPool), { jwtAudience: [staffClient.userPoolClientId] });
    const participantAuth = new HttpJwtAuthorizer('ParticipantJwt', issuer(participantPool), {
      jwtAudience: [participantClient.userPoolClientId],
    });

    const accessLogs = new logs.LogGroup(this, 'ApiAccessLogs', {
      retention: logs.RetentionDays.ONE_YEAR,
      encryptionKey: data.key,
    });
    const api = new apigwv2.HttpApi(this, 'Api', {
      apiName: 'trialtalk-api',
      createDefaultStage: false,
      corsPreflight: {
        allowOrigins: props.corsOrigins,
        allowMethods: [apigwv2.CorsHttpMethod.GET, apigwv2.CorsHttpMethod.POST, apigwv2.CorsHttpMethod.PATCH,
          apigwv2.CorsHttpMethod.DELETE],
        allowHeaders: ['authorization', 'content-type'],
        maxAge: Duration.hours(1),
      },
    });
    const stage = api.addStage('DefaultStage', {
      stageName: '$default',
      autoDeploy: true,
      throttle: { rateLimit: 50, burstLimit: 100 },
      accessLogSettings: {
        destination: new apigwv2.LogGroupLogDestination(accessLogs),
        format: AccessLogFormat.custom(JSON.stringify({
          requestId: '$context.requestId', routeKey: '$context.routeKey', status: '$context.status',
          sub: '$context.authorizer.claims.sub', ip: '$context.identity.sourceIp',
          latency: '$context.responseLatency', error: '$context.integrationErrorMessage',
        })),
      },
    });

    // No ANY: OPTIONS must fall through to the CORS preflight handler, not the authorizer.
    const methods = [apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST, apigwv2.HttpMethod.PATCH,
      apigwv2.HttpMethod.DELETE];
    api.addRoutes({
      path: '/staff/{proxy+}', methods, authorizer: staffAuth,
      integration: new HttpLambdaIntegration('StaffIntegration', staffFn),
    });
    const participantIntegration = new HttpLambdaIntegration('ParticipantIntegration', participantFn);
    for (const p of ['/me', '/me/{proxy+}']) {
      api.addRoutes({ path: p, methods, authorizer: participantAuth, integration: participantIntegration });
    }

    new CfnOutput(this, 'ApiUrl', { value: stage.url });
    new CfnOutput(this, 'StaffUserPoolId', { value: staffPool.userPoolId });
    new CfnOutput(this, 'StaffClientId', { value: staffClient.userPoolClientId });
    new CfnOutput(this, 'StaffLoginDomain', {
      value: `https://${props.cognitoDomainPrefix}.auth.${this.region}.amazoncognito.com`,
    });
    new CfnOutput(this, 'ParticipantUserPoolId', { value: participantPool.userPoolId });
    new CfnOutput(this, 'ParticipantClientId', { value: participantClient.userPoolClientId });
  }
}

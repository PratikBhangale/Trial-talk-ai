import * as path from 'path';
import * as fs from 'fs';
import * as crypto from 'crypto';
import { CustomResource, Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

export const LAMBDA_DIR = path.join(__dirname, '..', '..', 'lambdas');
export const DB_NAME = 'trialtalk';

/** Database logins, one per Lambda family; each joins exactly one group role (see schema.sql). */
export const LOGINS = ['staff_api', 'participant_api', 'messaging_api', 'agent_api'] as const;
export type Login = (typeof LOGINS)[number];

/**
 * network/data: KMS key, Aurora PostgreSQL Serverless v2 (Data API on, min 0.5 ACU),
 * a Secrets Manager login per Lambda family, and the schema migration.
 */
export class DataStack extends Stack {
  readonly key: kms.Key;
  readonly cluster: rds.DatabaseCluster;
  readonly loginSecrets: Record<Login, rds.DatabaseSecret>;

  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);

    this.key = new kms.Key(this, 'DataKey', {
      alias: 'alias/trialtalk-data',
      description: 'TrialTalk health data (Aurora, secrets, logs, schedules)',
      enableKeyRotation: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    // KMS-encrypted CloudWatch log groups
    this.key.grantEncryptDecrypt(new iam.ServicePrincipal(`logs.${this.region}.amazonaws.com`));

    // Lambdas reach Aurora through the Data API, so the VPC needs no NAT or internet access.
    const vpc = new ec2.Vpc(this, 'Vpc', {
      availabilityZones: [`${this.region}a`, `${this.region}b`], // no AZ lookup, so synth needs no credentials
      natGateways: 0,
      subnetConfiguration: [{ name: 'db', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 }],
    });

    const engine = rds.DatabaseClusterEngine.auroraPostgres({ version: rds.AuroraPostgresEngineVersion.VER_16_6 });
    const parameterGroup = new rds.ParameterGroup(this, 'Params', {
      engine,
      parameters: {
        shared_preload_libraries: 'pg_stat_statements,pgaudit',
        'pgaudit.log': 'write,ddl,role',
        'pgaudit.log_parameter': '0', // keep PHI values out of the audit log; statements still recorded
        'rds.force_ssl': '1',
      },
    });

    this.cluster = new rds.DatabaseCluster(this, 'Aurora', {
      engine,
      parameterGroup,
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      writer: rds.ClusterInstance.serverlessV2('writer'),
      serverlessV2MinCapacity: 0.5, // at 0 the cluster pauses and the first WhatsApp message waits
      serverlessV2MaxCapacity: 4,
      enableDataApi: true,
      defaultDatabaseName: DB_NAME,
      credentials: rds.Credentials.fromGeneratedSecret('tt_admin', { encryptionKey: this.key }),
      storageEncrypted: true,
      storageEncryptionKey: this.key,
      backup: { retention: Duration.days(7) },
      cloudwatchLogsExports: ['postgresql'],
      cloudwatchLogsRetention: logs.RetentionDays.ONE_YEAR,
      deletionProtection: true,
      removalPolicy: RemovalPolicy.SNAPSHOT,
    });

    this.loginSecrets = {} as Record<Login, rds.DatabaseSecret>;
    for (const login of LOGINS) {
      const secret = new rds.DatabaseSecret(this, `Login-${login}`, {
        username: login,
        secretName: `trialtalk/db/${login}`,
        encryptionKey: this.key,
        masterSecret: this.cluster.secret,
      });
      // attach() adds host/port/engine so the Data API accepts the secret
      secret.attach(this.cluster);
      this.loginSecrets[login] = secret;
    }

    // Migration: applies lambdas/migrate/schema.sql on every deploy where it changed, then creates the logins.
    const migrateFn = new lambda.Function(this, 'MigrateFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'migrate.handler.handler',
      code: lambda.Code.fromAsset(LAMBDA_DIR, { exclude: ['**/__pycache__'] }),
      timeout: Duration.minutes(5),
      memorySize: 256,
      environment: {
        DB_CLUSTER_ARN: this.cluster.clusterArn,
        DB_MASTER_SECRET_ARN: this.cluster.secret!.secretArn,
        DB_NAME,
        LOGIN_SECRETS: this.toJsonString(
          Object.fromEntries(LOGINS.map((l) => [l, this.loginSecrets[l].secretArn])),
        ),
      },
      logGroup: new logs.LogGroup(this, 'MigrateLogs', { retention: logs.RetentionDays.ONE_MONTH }),
    });
    this.cluster.grantDataApiAccess(migrateFn);
    for (const login of LOGINS) this.loginSecrets[login].grantRead(migrateFn);

    const schemaHash = crypto
      .createHash('sha256')
      .update(fs.readFileSync(path.join(LAMBDA_DIR, 'migrate', 'schema.sql')))
      .update(fs.readFileSync(path.join(LAMBDA_DIR, 'migrate', 'handler.py')))
      .digest('hex');
    const provider = new cr.Provider(this, 'MigrateProvider', { onEventHandler: migrateFn });
    const migration = new CustomResource(this, 'Schema', {
      serviceToken: provider.serviceToken,
      properties: { schemaHash },
    });
    migration.node.addDependency(this.cluster);
    for (const login of LOGINS) migration.node.addDependency(this.loginSecrets[login]);
  }
}

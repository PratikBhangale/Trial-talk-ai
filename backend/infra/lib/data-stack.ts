import * as path from 'path';
import { RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';

export const LAMBDA_DIR = path.join(__dirname, '..', '..', 'lambdas');
export const DB_NAME = 'trialtalk';

/** Database logins, one per Lambda family; each joins exactly one group role (see db/02_roles_rls.sql). */
export const LOGINS = ['staff_api', 'participant_api', 'messaging_api', 'agent_api'] as const;
export type Login = (typeof LOGINS)[number];

/**
 * data: KMS key for the Lambdas' environment and logs, plus references to the existing Aurora PostgreSQL
 * cluster (created in the console, migrated with db/run_sql.py) and its per-Lambda login secrets.
 * Context: dbClusterArn, and dbLoginSecretArns as {login: full secret ARN}. The cluster may be in another
 * region; the Lambdas call the Data API there through DB_REGION.
 */
export class DataStack extends Stack {
  readonly key: kms.Key;
  readonly clusterArn: string;
  readonly dbRegion: string;
  readonly loginSecrets: Record<Login, secretsmanager.ISecret>;

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

    this.clusterArn = this.node.getContext('dbClusterArn');
    this.dbRegion = this.clusterArn.split(':')[3];
    // Full ARNs (with the -XXXXXX suffix): IAM policies and the Data API need the exact secret.
    const secretArns = this.node.getContext('dbLoginSecretArns') as Record<Login, string>;
    this.loginSecrets = Object.fromEntries(
      LOGINS.map((l) => [l, secretsmanager.Secret.fromSecretCompleteArn(this, `Login-${l}`, secretArns[l])]),
    ) as Record<Login, secretsmanager.ISecret>;
  }
}

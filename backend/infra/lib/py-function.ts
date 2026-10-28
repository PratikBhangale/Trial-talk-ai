import { Duration } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { DB_NAME, DataStack, LAMBDA_DIR, Login } from './data-stack';

export interface PyFunctionProps {
  handler: string;
  data: DataStack;
  /** Database login this function connects as (omit for functions that never touch Aurora). */
  login?: Login;
  environment?: Record<string, string>;
  timeout?: Duration;
  memorySize?: number;
  layers?: lambda.ILayerVersion[];
}

/** Python 3.12 Lambda from backend/lambdas, X-Ray on, KMS-encrypted env and logs. */
export class PyFunction extends lambda.Function {
  constructor(scope: Construct, id: string, props: PyFunctionProps) {
    const { data, login } = props;
    super(scope, id, {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: props.handler,
      code: lambda.Code.fromAsset(LAMBDA_DIR, { exclude: ['**/__pycache__', 'migrate/**'] }),
      timeout: props.timeout ?? Duration.seconds(15),
      memorySize: props.memorySize ?? 256,
      layers: props.layers,
      tracing: lambda.Tracing.ACTIVE,
      environmentEncryption: data.key,
      environment: {
        ...(login
          ? {
              DB_CLUSTER_ARN: data.clusterArn,
              DB_SECRET_ARN: data.loginSecrets[login].secretArn,
              DB_NAME,
              DB_REGION: data.dbRegion,
            }
          : {}),
        ...props.environment,
      },
      logGroup: new logs.LogGroup(scope, `${id}Logs`, {
        retention: logs.RetentionDays.ONE_YEAR,
        encryptionKey: data.key,
      }),
    });
    if (login) {
      // Only this login's secret, never the master secret, which would bypass RLS.
      this.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            'rds-data:ExecuteStatement',
            'rds-data:BatchExecuteStatement',
            'rds-data:BeginTransaction',
            'rds-data:CommitTransaction',
            'rds-data:RollbackTransaction',
          ],
          resources: [data.clusterArn],
        }),
      );
      this.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
          resources: [data.loginSecrets[login].secretArn],
        }),
      );
    }
    // Identity-based (not key.grantDecrypt) so the Data stack's key policy never references other stacks.
    this.addToRolePolicy(new iam.PolicyStatement({ actions: ['kms:Decrypt'], resources: [data.key.keyArn] }));
  }
}

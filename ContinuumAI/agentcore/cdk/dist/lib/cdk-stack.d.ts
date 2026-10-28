import { AgentCoreApplication, type AgentCoreProjectSpec, type AgentCoreMcpSpec, type CustomJWTAuthorizerConfig, type HarnessDeploymentConfig } from '@aws/agentcore-cdk';
import { Stack, type StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';
/**
 * Harness deployment config: role-scoped fields (for IAM role + container build)
 * plus the full validated spec + its config directory so the L3 construct can
 * synthesize the AWS::BedrockAgentCore::Harness resource.
 */
export type HarnessConfig = HarnessDeploymentConfig;
export interface ManualPaymentConnectorSpec {
    name: string;
    provider: 'CoinbaseCDP' | 'StripePrivy';
    provisionMode?: 'MANUAL';
    credentialName: string;
    credentialProviderArn: string;
}
export interface QuickCreatePaymentConnectorSpec {
    name: string;
    provider: 'CoinbaseCDP';
    provisionMode: 'QUICK_CREATE';
    credentialName?: never;
    credentialProviderArn?: never;
}
export type PaymentConnectorSpec = ManualPaymentConnectorSpec | QuickCreatePaymentConnectorSpec;
export interface PaymentSpec {
    name: string;
    description?: string;
    authorizerType: 'AWS_IAM' | 'CUSTOM_JWT';
    authorizerConfiguration?: {
        customJWTAuthorizer: CustomJWTAuthorizerConfig;
    };
    autoPayment?: boolean;
    paymentToolAllowlist?: string[];
    networkPreferences?: string[];
    connectors: PaymentConnectorSpec[];
}
export interface AgentCoreStackProps extends StackProps {
    /**
     * The AgentCore project specification containing agents, memories, and credentials.
     */
    spec: AgentCoreProjectSpec;
    /**
     * The MCP specification containing gateways and servers.
     */
    mcpSpec?: AgentCoreMcpSpec;
    /**
     * Credential provider ARNs from deployed state, keyed by credential name.
     */
    credentials?: Record<string, {
        credentialProviderArn: string;
        clientSecretArn?: string;
    }>;
    /**
     * Harness role configurations.
     */
    harnesses?: HarnessConfig[];
    /**
     * Parsed connectorParameters for non-S3 KB data sources, keyed by
     * connectorConfigFile path. Forwarded to AgentCoreApplication.
     */
    connectorParametersByFile?: Record<string, Record<string, unknown>>;
    /**
     * Payment specifications with resolved credential provider ARNs.
     */
    paymentSpec?: PaymentSpec[];
}
/**
 * CDK Stack that deploys AgentCore infrastructure.
 *
 * This is a thin wrapper that instantiates L3 constructs.
 * All resource logic and outputs are contained within the L3 constructs.
 */
export declare class AgentCoreStack extends Stack {
    /** The AgentCore application containing all agent environments */
    readonly application: AgentCoreApplication;
    constructor(scope: Construct, id: string, props: AgentCoreStackProps);
}

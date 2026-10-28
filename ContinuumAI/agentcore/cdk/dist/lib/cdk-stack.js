"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.AgentCoreStack = void 0;
const agentcore_cdk_1 = require("@aws/agentcore-cdk");
const aws_cdk_lib_1 = require("aws-cdk-lib");
const iam = require("aws-cdk-lib/aws-iam");
function toCdkId(name) {
    return name.replace(/_/g, '');
}
/**
 * Decide whether a deployed runtime should receive payment env vars + IAM grants.
 * Payments today only ships a runtime shim for Python HTTP runtimes; injecting
 * AGENTCORE_PAYMENT_* env vars into TypeScript / MCP / A2A / AGUI runtimes
 * would surface env vars they cannot consume and would dilute least-privilege
 * IAM grants for runtimes that never call ProcessPayment.
 */
function isPaymentEligibleAgent(agent) {
    if (agent.protocol && agent.protocol !== 'HTTP') {
        return false;
    }
    const entrypoint = typeof agent.entrypoint === 'string' ? agent.entrypoint : '';
    const entrypointFile = entrypoint.split(':')[0] ?? '';
    return entrypointFile.endsWith('.py');
}
/**
 * CDK Stack that deploys AgentCore infrastructure.
 *
 * This is a thin wrapper that instantiates L3 constructs.
 * All resource logic and outputs are contained within the L3 constructs.
 */
class AgentCoreStack extends aws_cdk_lib_1.Stack {
    /** The AgentCore application containing all agent environments */
    application;
    constructor(scope, id, props) {
        super(scope, id, props);
        const { spec, mcpSpec, credentials, harnesses, connectorParametersByFile, paymentSpec } = props;
        // Create AgentCoreApplication with all agents and harness roles
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const appProps = { spec };
        if (harnesses?.length) {
            appProps.harnesses = harnesses;
        }
        if (connectorParametersByFile && Object.keys(connectorParametersByFile).length > 0) {
            appProps.connectorParametersByFile = connectorParametersByFile;
        }
        if (credentials) {
            appProps.credentials = credentials;
        }
        this.application = new agentcore_cdk_1.AgentCoreApplication(this, 'Application', appProps);
        // Create AgentCoreMcp if there are gateways configured
        if (mcpSpec?.agentCoreGateways && mcpSpec.agentCoreGateways.length > 0) {
            new agentcore_cdk_1.AgentCoreMcp(this, 'Mcp', {
                projectName: spec.name,
                mcpSpec,
                agentCoreApplication: this.application,
                credentials,
                projectTags: spec.tags,
            });
        }
        // Create payment infrastructure via CFN constructs
        if (paymentSpec && paymentSpec.length > 0) {
            for (const payment of paymentSpec) {
                const mgrId = toCdkId(payment.name);
                const manager = new agentcore_cdk_1.AgentCorePaymentManager(this, `Payment${mgrId}`, {
                    projectName: spec.name,
                    name: payment.name,
                    authorizerType: payment.authorizerType,
                    description: payment.description,
                    authorizerConfiguration: payment.authorizerConfiguration,
                    tags: spec.tags,
                });
                const prefix = `AGENTCORE_PAYMENT_${payment.name.toUpperCase().replace(/-/g, '_')}`;
                // Wire env vars from construct output tokens into eligible agent environments only.
                // See isPaymentEligibleAgent — non-Python or non-HTTP runtimes have no shim that
                // can consume these env vars, and giving them sts:AssumeRole on the
                // ProcessPaymentRole would broaden the privilege surface unnecessarily.
                for (const env of this.application.environments.values()) {
                    if (!isPaymentEligibleAgent(env.agent)) {
                        continue;
                    }
                    env.runtime.addEnvironmentVariable(`${prefix}_MANAGER_ARN`, manager.paymentManagerArn);
                    env.runtime.addEnvironmentVariable(`${prefix}_PROCESS_PAYMENT_ROLE_ARN`, manager.processPaymentRoleArn);
                    // Grant runtime execution role permission to assume the ProcessPaymentRole.
                    // The ProcessPaymentRole's trust policy allows AccountRootPrincipal, but the
                    // caller still needs sts:AssumeRole on its own role to perform the assumption.
                    env.runtime.role.addToPrincipalPolicy(new iam.PolicyStatement({
                        actions: ['sts:AssumeRole'],
                        resources: [manager.processPaymentRoleArn],
                    }));
                    // Grant payment data-plane actions directly to the runtime role.
                    //
                    // NOTE: This deviates from the canonical role model in the AgentCore Payments
                    // beta guide, which assigns Get/List/Create instrument+session actions to a
                    // separate ManagementRole and limits the agent's role to ProcessPayment only.
                    // The current SDK plugin (AgentCorePaymentsPlugin.generate_payment_header)
                    // calls GetPaymentInstrument internally during the 402 auto-pay path, so the
                    // runtime role needs read access. CreatePaymentSession is included so
                    // `agentcore invoke --auto-session` works without a separate ManagementRole
                    // call. Tighten this if the SDK is updated to accept pre-fetched instrument
                    // details and split create-session into a backend-only flow.
                    env.runtime.role.addToPrincipalPolicy(new iam.PolicyStatement({
                        actions: [
                            'bedrock-agentcore:GetPaymentInstrument',
                            'bedrock-agentcore:ListPaymentInstruments',
                            'bedrock-agentcore:GetPaymentInstrumentBalance',
                            'bedrock-agentcore:GetPaymentSession',
                            'bedrock-agentcore:ListPaymentSessions',
                            'bedrock-agentcore:CreatePaymentSession',
                            'bedrock-agentcore:ProcessPayment',
                        ],
                        resources: [manager.paymentManagerArn, `${manager.paymentManagerArn}/*`],
                    }));
                    if (payment.autoPayment !== undefined) {
                        env.runtime.addEnvironmentVariable(`${prefix}_AUTO_PAYMENT`, String(payment.autoPayment));
                    }
                    if (payment.paymentToolAllowlist) {
                        env.runtime.addEnvironmentVariable(`${prefix}_TOOL_ALLOWLIST`, payment.paymentToolAllowlist.join(','));
                    }
                    if (payment.networkPreferences) {
                        env.runtime.addEnvironmentVariable(`${prefix}_NETWORK_PREFERENCES`, payment.networkPreferences.join(','));
                    }
                    if (payment.authorizerType === 'CUSTOM_JWT') {
                        env.runtime.addEnvironmentVariable(`${prefix}_AUTH_MODE`, 'bearer');
                    }
                }
                // Create connectors for this manager
                for (const connector of payment.connectors) {
                    const connId = toCdkId(connector.name);
                    const schemaConnector = connector.provisionMode === 'QUICK_CREATE'
                        ? connector
                        : {
                            name: connector.name,
                            provider: connector.provider,
                            ...(connector.provisionMode && { provisionMode: connector.provisionMode }),
                            credentialName: connector.credentialName,
                        };
                    const compatibilityProps = {
                        projectName: spec.name,
                        paymentManager: manager,
                        connector: schemaConnector,
                        // Remove these legacy manual fields after the new L3 release is pinned.
                        connectorName: connector.name,
                        connectorType: connector.provider,
                        ...(connector.provisionMode !== 'QUICK_CREATE' && {
                            credentialProviderArn: connector.credentialProviderArn,
                        }),
                    };
                    const conn = new agentcore_cdk_1.AgentCorePaymentConnector(this, `Payment${mgrId}${connId}`, compatibilityProps);
                    // Wire first connector's ID as env var (eligible agents only)
                    if (connector === payment.connectors[0]) {
                        for (const env of this.application.environments.values()) {
                            if (!isPaymentEligibleAgent(env.agent))
                                continue;
                            env.runtime.addEnvironmentVariable(`${prefix}_CONNECTOR_ID`, conn.paymentConnectorId);
                        }
                    }
                    new aws_cdk_lib_1.CfnOutput(this, `Payment${mgrId}${connId}ConnectorId`, {
                        value: conn.paymentConnectorId,
                    });
                    if (connector.provisionMode === 'QUICK_CREATE') {
                        const quickCreateConnector = conn;
                        new aws_cdk_lib_1.CfnOutput(this, `Payment${mgrId}${connId}ConnectorStatus`, {
                            value: quickCreateConnector.paymentConnectorStatus,
                        });
                        new aws_cdk_lib_1.CfnOutput(this, `Payment${mgrId}${connId}AuthorizationUrl`, {
                            value: quickCreateConnector.authorizationUrl,
                        });
                    }
                }
                // CFN Outputs for post-deploy state parsing
                new aws_cdk_lib_1.CfnOutput(this, `Payment${mgrId}ManagerArn`, {
                    value: manager.paymentManagerArn,
                });
                new aws_cdk_lib_1.CfnOutput(this, `Payment${mgrId}ManagerId`, {
                    value: manager.paymentManagerId,
                });
                new aws_cdk_lib_1.CfnOutput(this, `Payment${mgrId}ProcessPaymentRoleArn`, {
                    value: manager.processPaymentRoleArn,
                });
                new aws_cdk_lib_1.CfnOutput(this, `Payment${mgrId}ResourceRetrievalRoleArn`, {
                    value: manager.resourceRetrievalRoleArn,
                });
            }
        }
        // Stack-level output
        new aws_cdk_lib_1.CfnOutput(this, 'StackNameOutput', {
            description: 'Name of the CloudFormation Stack',
            value: this.stackName,
        });
    }
}
exports.AgentCoreStack = AgentCoreStack;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiY2RrLXN0YWNrLmpzIiwic291cmNlUm9vdCI6IiIsInNvdXJjZXMiOlsiLi4vLi4vbGliL2Nkay1zdGFjay50cyJdLCJuYW1lcyI6W10sIm1hcHBpbmdzIjoiOzs7QUFBQSxzREFTNEI7QUFDNUIsNkNBQWdFO0FBQ2hFLDJDQUEyQztBQW1FM0MsU0FBUyxPQUFPLENBQUMsSUFBWTtJQUMzQixPQUFPLElBQUksQ0FBQyxPQUFPLENBQUMsSUFBSSxFQUFFLEVBQUUsQ0FBQyxDQUFDO0FBQ2hDLENBQUM7QUFFRDs7Ozs7O0dBTUc7QUFDSCxTQUFTLHNCQUFzQixDQUFDLEtBQWlEO0lBQy9FLElBQUksS0FBSyxDQUFDLFFBQVEsSUFBSSxLQUFLLENBQUMsUUFBUSxLQUFLLE1BQU0sRUFBRSxDQUFDO1FBQ2hELE9BQU8sS0FBSyxDQUFDO0lBQ2YsQ0FBQztJQUNELE1BQU0sVUFBVSxHQUFHLE9BQU8sS0FBSyxDQUFDLFVBQVUsS0FBSyxRQUFRLENBQUMsQ0FBQyxDQUFDLEtBQUssQ0FBQyxVQUFVLENBQUMsQ0FBQyxDQUFDLEVBQUUsQ0FBQztJQUNoRixNQUFNLGNBQWMsR0FBRyxVQUFVLENBQUMsS0FBSyxDQUFDLEdBQUcsQ0FBQyxDQUFDLENBQUMsQ0FBQyxJQUFJLEVBQUUsQ0FBQztJQUN0RCxPQUFPLGNBQWMsQ0FBQyxRQUFRLENBQUMsS0FBSyxDQUFDLENBQUM7QUFDeEMsQ0FBQztBQUVEOzs7OztHQUtHO0FBQ0gsTUFBYSxjQUFlLFNBQVEsbUJBQUs7SUFDdkMsa0VBQWtFO0lBQ2xELFdBQVcsQ0FBdUI7SUFFbEQsWUFBWSxLQUFnQixFQUFFLEVBQVUsRUFBRSxLQUEwQjtRQUNsRSxLQUFLLENBQUMsS0FBSyxFQUFFLEVBQUUsRUFBRSxLQUFLLENBQUMsQ0FBQztRQUV4QixNQUFNLEVBQUUsSUFBSSxFQUFFLE9BQU8sRUFBRSxXQUFXLEVBQUUsU0FBUyxFQUFFLHlCQUF5QixFQUFFLFdBQVcsRUFBRSxHQUFHLEtBQUssQ0FBQztRQUVoRyxnRUFBZ0U7UUFDaEUs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QyxJQUFJLEVBQUUsVUFBVSxLQUFLLFlBQVksRUFBRTtvQkFDL0MsS0FBSyxFQUFFLE9BQU8sQ0FBQyxpQkFBaUI7aUJBQ2pDLENBQUMsQ0FBQztnQkFDSCxJQUFJLHVCQUFTLENBQUMsSUFBSSxFQUFFLFVBQVUsS0FBSyxXQUFXLEVBQUU7b0JBQzlDLEtBQUssRUFBRSxPQUFPLENBQUMsZ0JBQWdCO2lCQUNoQyxDQUFDLENBQUM7Z0JBQ0gsSUFBSSx1QkFBUyxDQUFDLElBQUksRUFBRSxVQUFVLEtBQUssdUJBQXVCLEVBQUU7b0JBQzFELEtBQUssRUFBRSxPQUFPLENBQUMscUJBQXFCO2lCQUNyQyxDQUFDLENBQUM7Z0JBQ0gsSUFBSSx1QkFBUyxDQUFDLElBQUksRUFBRSxVQUFVLEtBQUssMEJBQTBCLEVBQUU7b0JBQzdELEtBQUssRUFBRSxPQUFPLENBQUMsd0JBQXdCO2lCQUN4QyxDQUFDLENBQUM7WUFDTCxDQUFDO1FBQ0gsQ0FBQztRQUVELHFCQUFxQjtRQUNyQixJQUFJLHVCQUFTLENBQUMsSUFBSSxFQUFFLGlCQUFpQixFQUFFO1lBQ3JDLFdBQVcsRUFBRSxrQ0FBa0M7WUFDL0MsS0FBSyxFQUFFLElBQUksQ0FBQyxTQUFTO1NBQ3RCLENBQUMsQ0FBQztJQUNMLENBQUM7Q0FDRjtBQTFMRCx3Q0EwTEMiLCJzb3VyY2VzQ29udGVudCI6WyJpbXBvcnQge1xuICBBZ2VudENvcmVBcHBsaWNhdGlvbixcbiAgQWdlbnRDb3JlTWNwLFxuICBBZ2VudENvcmVQYXltZW50TWFuYWdlcixcbiAgQWdlbnRDb3JlUGF5bWVudENvbm5lY3RvcixcbiAgdHlwZSBBZ2VudENvcmVQcm9qZWN0U3BlYyxcbiAgdHlwZSBBZ2VudENvcmVNY3BTcGVjLFxuICB0eXBlIEN1c3RvbUpXVEF1dGhvcml6ZXJDb25maWcsXG4gIHR5cGUgSGFybmVzc0RlcGxveW1lbnRDb25maWcsXG59IGZyb20gJ0Bhd3MvYWdlbnRjb3JlLWNkayc7XG5pbXBvcnQgeyBDZm5PdXRwdXQsIFN0YWNrLCB0eXBlIFN0YWNrUHJvcHMgfSBmcm9tICdhd3MtY2RrLWxpYic7XG5pbXBvcnQgKiBhcyBpYW0gZnJvbSAnYXdzLWNkay1saWIvYXdzLWlhbSc7XG5pbXBvcnQgeyBDb25zdHJ1Y3QgfSBmcm9tICdjb25zdHJ1Y3RzJztcblxuLyoqXG4gKiBIYXJuZXNzIGRlcGxveW1lbnQgY29uZmlnOiByb2xlLXNjb3BlZCBmaWVsZHMgKGZvciBJQU0gcm9sZSArIGNvbnRhaW5lciBidWlsZClcbiAqIHBsdXMgdGhlIGZ1bGwgdmFsaWRhdGVkIHNwZWMgKyBpdHMgY29uZmlnIGRpcmVjdG9yeSBzbyB0aGUgTDMgY29uc3RydWN0IGNhblxuICogc3ludGhlc2l6ZSB0aGUgQVdTOjpCZWRyb2NrQWdlbnRDb3JlOjpIYXJuZXNzIHJlc291cmNlLlxuICovXG5leHBvcnQgdHlwZSBIYXJuZXNzQ29uZmlnID0gSGFybmVzc0RlcGxveW1lbnRDb25maWc7XG5cbmV4cG9ydCBpbnRlcmZhY2UgTWFudWFsUGF5bWVudENvbm5lY3RvclNwZWMge1xuICBuYW1lOiBzdHJpbmc7XG4gIHByb3ZpZGVyOiAnQ29pbmJhc2VDRFAnIHwgJ1N0cmlwZVByaXZ5JztcbiAgcHJvdmlzaW9uTW9kZT86ICdNQU5VQUwnO1xuICBjcmVkZW50aWFsTmFtZTogc3RyaW5nO1xuICBjcmVkZW50aWFsUHJvdmlkZXJBcm46IHN0cmluZztcbn1cblxuZXhwb3J0IGludGVyZmFjZSBRdWlja0NyZWF0ZVBheW1lbnRDb25uZWN0b3JTcGVjIHtcbiAgbmFtZTogc3RyaW5nO1xuICBwcm92aWRlcjogJ0NvaW5iYXNlQ0RQJztcbiAgcHJvdmlzaW9uTW9kZTogJ1FVSUNLX0NSRUFURSc7XG4gIGNyZWRlbnRpYWxOYW1lPzogbmV2ZXI7XG4gIGNyZWRlbnRpYWxQcm92aWRlckFybj86IG5ldmVyO1xufVxuXG5leHBvcnQgdHlwZSBQYXltZW50Q29ubmVjdG9yU3BlYyA9IE1hbnVhbFBheW1lbnRDb25uZWN0b3JTcGVjIHwgUXVpY2tDcmVhdGVQYXltZW50Q29ubmVjdG9yU3BlYztcblxuZXhwb3J0IGludGVyZmFjZSBQYXltZW50U3BlYyB7XG4gIG5hbWU6IHN0cmluZztcbiAgZGVzY3JpcHRpb24/OiBzdHJpbmc7XG4gIGF1dGhvcml6ZXJUeXBlOiAnQVdTX0lBTScgfCAnQ1VTVE9NX0pXVCc7XG4gIGF1dGhvcml6ZXJDb25maWd1cmF0aW9uPzogeyBjdXN0b21KV1RBdXRob3JpemVyOiBDdXN0b21KV1RBdXRob3JpemVyQ29uZmlnIH07XG4gIGF1dG9QYXltZW50PzogYm9vbGVhbjtcbiAgcGF5bWVudFRvb2xBbGxvd2xpc3Q/OiBzdHJpbmdbXTtcbiAgbmV0d29ya1ByZWZlcmVuY2VzPzogc3RyaW5nW107XG4gIGNvbm5lY3RvcnM6IFBheW1lbnRDb25uZWN0b3JTcGVjW107XG59XG5cbmV4cG9ydCBpbnRlcmZhY2UgQWdlbnRDb3JlU3RhY2tQcm9wcyBleHRlbmRzIFN0YWNrUHJvcHMge1xuICAvKipcbiAgICogVGhlIEFnZW50Q29yZSBwcm9qZWN0IHNwZWNpZmljYXRpb24gY29udGFpbmluZyBhZ2VudHMsIG1lbW9yaWVzLCBhbmQgY3JlZGVudGlhbHMuXG4gICAqL1xuICBzcGVjOiBBZ2VudENvcmVQcm9qZWN0U3BlYztcbiAgLyoqXG4gICAqIFRoZSBNQ1Agc3BlY2lmaWNhdGlvbiBjb250YWluaW5nIGdhdGV3YXlzIGFuZCBzZXJ2ZXJzLlxuICAgKi9cbiAgbWNwU3BlYz86IEFnZW50Q29yZU1jcFNwZWM7XG4gIC8qKlxuICAgKiBDcmVkZW50aWFsIHByb3ZpZGVyIEFSTnMgZnJvbSBkZXBsb3llZCBzdGF0ZSwga2V5ZWQgYnkgY3JlZGVudGlhbCBuYW1lLlxuICAgKi9cbiAgY3JlZGVudGlhbHM/OiBSZWNvcmQ8c3RyaW5nLCB7IGNyZWRlbnRpYWxQcm92aWRlckFybjogc3RyaW5nOyBjbGllbnRTZWNyZXRBcm4/OiBzdHJpbmcgfT47XG4gIC8qKlxuICAgKiBIYXJuZXNzIHJvbGUgY29uZmlndXJhdGlvbnMuXG4gICAqL1xuICBoYXJuZXNzZXM/OiBIYXJuZXNzQ29uZmlnW107XG4gIC8qKlxuICAgKiBQYXJzZWQgY29ubmVjdG9yUGFyYW1ldGVycyBmb3Igbm9uLVMzIEtCIGRhdGEgc291cmNlcywga2V5ZWQgYnlcbiAgICogY29ubmVjdG9yQ29uZmlnRmlsZSBwYXRoLiBGb3J3YXJkZWQgdG8gQWdlbnRDb3JlQXBwbGljYXRpb24uXG4gICAqL1xuICBjb25uZWN0b3JQYXJhbWV0ZXJzQnlGaWxlPzogUmVjb3JkPHN0cmluZywgUmVjb3JkPHN0cmluZywgdW5rbm93bj4+O1xuICAvKipcbiAgICogUGF5bWVudCBzcGVjaWZpY2F0aW9ucyB3aXRoIHJlc29sdmVkIGNyZWRlbnRpYWwgcHJvdmlkZXIgQVJOcy5cbiAgICovXG4gIHBheW1lbnRTcGVjPzogUGF5bWVudFNwZWNbXTtcbn1cblxuZnVuY3Rpb24gdG9DZGtJZChuYW1lOiBzdHJpbmcpOiBzdHJpbmcge1xuICByZXR1cm4gbmFtZS5yZXBsYWNlKC9fL2csICcnKTtcbn1cblxuLyoqXG4gKiBEZWNpZGUgd2hldGhlciBhIGRlcGxveWVkIHJ1bnRpbWUgc2hvdWxkIHJlY2VpdmUgcGF5bWVudCBlbnYgdmFycyArIElBTSBncmFudHMuXG4gKiBQYXltZW50cyB0b2RheSBvbmx5IHNoaXBzIGEgcnVudGltZSBzaGltIGZvciBQeXRob24gSFRUUCBydW50aW1lczsgaW5qZWN0aW5nXG4gKiBBR0VOVENPUkVfUEFZTUVOVF8qIGVudiB2YXJzIGludG8gVHlwZVNjcmlwdCAvIE1DUCAvIEEyQSAvIEFHVUkgcnVudGltZXNcbiAqIHdvdWxkIHN1cmZhY2UgZW52IHZhcnMgdGhleSBjYW5ub3QgY29uc3VtZSBhbmQgd291bGQgZGlsdXRlIGxlYXN0LXByaXZpbGVnZVxuICogSUFNIGdyYW50cyBmb3IgcnVudGltZXMgdGhhdCBuZXZlciBjYWxsIFByb2Nlc3NQYXltZW50LlxuICovXG5mdW5jdGlvbiBpc1BheW1lbnRFbGlnaWJsZUFnZW50KGFnZW50OiB7IGVudHJ5cG9pbnQ/OiBzdHJpbmc7IHByb3RvY29sPzogc3RyaW5nIH0pOiBib29sZWFuIHtcbiAgaWYgKGFnZW50LnByb3RvY29sICYmIGFnZW50LnByb3RvY29sICE9PSAnSFRUUCcpIHtcbiAgICByZXR1cm4gZmFsc2U7XG4gIH1cbiAgY29uc3QgZW50cnlwb2ludCA9IHR5cGVvZiBhZ2VudC5lbnRyeXBvaW50ID09PSAnc3RyaW5nJyA/IGFnZW50LmVudHJ5cG9pbnQgOiAnJztcbiAgY29uc3QgZW50cnlwb2ludEZpbGUgPSBlbnRyeXBvaW50LnNwbGl0KCc6JylbMF0gPz8gJyc7XG4gIHJldHVybiBlbnRyeXBvaW50RmlsZS5lbmRzV2l0aCgnLnB5Jyk7XG59XG5cbi8qKlxuICogQ0RLIFN0YWNrIHRoYXQgZGVwbG95cyBBZ2VudENvcmUgaW5mcmFzdHJ1Y3R1cmUuXG4gKlxuICogVGhpcyBpcyBhIHRoaW4gd3JhcHBlciB0aGF0IGluc3RhbnRpYXRlcyBMMyBjb25zdHJ1Y3RzLlxuICogQWxsIHJlc291cmNlIGxvZ2ljIGFuZCBvdXRwdXRzIGFyZSBjb250YWluZWQgd2l0aGluIHRoZSBMMyBjb25zdHJ1Y3RzLlxuICovXG5leHBvcnQgY2xhc3MgQWdlbnRDb3JlU3RhY2sgZXh0ZW5kcyBTdGFjayB7XG4gIC8qKiBUaGUgQWdlbnRDb3JlIGFwcGxpY2F0aW9uIGNvbnRhaW5pbmcgYWxsIGFnZW50IGVudmlyb25tZW50cyAqL1xuICBwdWJsaWMgcmVhZG9ubHkgYXBwbGljYXRpb246IEFnZW50Q29yZUFwcGxpY2F0aW9uO1xuXG4gIGNvbnN0cnVjdG9yKHNjb3BlOiBDb25zdHJ1Y3QsIGlkOiBzdHJpbmcsIHByb3BzOiBBZ2VudENvcmVTdGFja1Byb3BzKSB7XG4gICAgc3VwZXIoc2NvcGUsIGlkLCBwcm9wcyk7XG5cbiAgICBjb25zdCB7IHNwZWMsIG1jcFNwZWMsIGNyZWRlbnRpYWxzLCBoYXJuZXNzZXMsIGNvbm5lY3RvclBhcmFtZXRlcnNCeUZpbGUsIHBheW1lbnRTcGVjIH0gPSBwcm9wcztcblxuICAgIC8vIENyZWF0ZSBBZ2VudENvcmVBcHBsaWNhdGlvbiB3aXRoIGFsbCBhZ2VudHMgYW5kIGhhcm5lc3Mgcm9sZXNcbiAgICAvLyBlc2xpbnQtZGlzYWJsZS1uZXh0LWxpbmUgQHR5cGVzY3JpcHQtZXNsaW50L25vLWV4cGxpY2l0LWFueVxuICAgIGNvbnN0IGFwcFByb3BzOiBSZWNvcmQ8c3RyaW5nLCB1bmtub3duPiA9IHsgc3BlYyB9O1xuICAgIGlmIChoYXJuZXNzZXM/Lmxlbmd0aCkge1xuICAgICAgYXBwUHJvcHMuaGFybmVzc2VzID0gaGFybmVzc2VzO1xuICAgIH1cbiAgICBpZiAoY29ubmVjdG9yUGFyYW1ldGVyc0J5RmlsZSAmJiBPYmplY3Qua2V5cyhjb25uZWN0b3JQYXJhbWV0ZXJzQnlGaWxlKS5sZW5ndGggPiAwKSB7XG4gICAgICBhcHBQcm9wcy5jb25uZWN0b3JQYXJhbWV0ZXJzQnlGaWxlID0gY29ubmVjdG9yUGFyYW1ldGVyc0J5RmlsZTtcbiAgICB9XG4gICAgaWYgKGNyZWRlbnRpYWxzKSB7XG4gICAgICBhcHBQcm9wcy5jcmVkZW50aWFscyA9IGNyZWRlbnRpYWxzO1xuICAgIH1cbiAgICB0aGlzLmFwcGxpY2F0aW9uID0gbmV3IEFnZW50Q29yZUFwcGxpY2F0aW9uKHRoaXMsICdBcHBsaWNhdGlvbicsIGFwcFByb3BzIGFzIGFueSk7XG5cbiAgICAvLyBDcmVhdGUgQWdlbnRDb3JlTWNwIGlmIHRoZXJlIGFyZSBnYXRld2F5cyBjb25maWd1cmVkXG4gICAgaWYgKG1jcFNwZWM/LmFnZW50Q29yZUdhdGV3YXlzICYmIG1jcFNwZWMuYWdlbnRDb3JlR2F0ZXdheXMubGVuZ3RoID4gMCkge1xuICAgICAgbmV3IEFnZW50Q29yZU1jcCh0aGlzLCAnTWNwJywge1xuICAgICAgICBwcm9qZWN0TmFtZTogc3BlYy5uYW1lLFxuICAgICAgICBtY3BTcGVjLFxuICAgICAgICBhZ2VudENvcmVBcHBsaWNhdGlvbjogdGhpcy5hcHBsaWNhdGlvbixcbiAgICAgICAgY3JlZGVudGlhbHMsXG4gICAgICAgIHByb2plY3RUYWdzOiBzcGVjLnRhZ3MsXG4gICAgICB9KTtcbiAgICB9XG5cbiAgICAvLyBDcmVhdGUgcGF5bWVudCBpbmZyYXN0cnVjdHVyZSB2aWEgQ0ZOIGNvbnN0cnVjdHNcbiAgICBpZiAocGF5bWVudFNwZWMgJiYgcGF5bWVudFNwZWMubGVuZ3RoID4gMCkge1xuICAgICAgZm9yIChjb25zdCBwYXltZW50IG9mIHBheW1lbnRTcGVjKSB7XG4gICAgICAgIGNvbnN0IG1ncklkID0gdG9DZGtJZChwYXltZW50Lm5hbWUpO1xuICAgICAgICBjb25zdCBtYW5hZ2VyID0gbmV3IEFnZW50Q29yZVBheW1lbnRNYW5hZ2VyKHRoaXMsIGBQYXltZW50JHttZ3JJZH1gLCB7XG4gICAgICAgICAgcHJvamVjdE5hbWU6IHNwZWMubmFtZSxcbiAgICAgICAgICBuYW1lOiBwYXltZW50Lm5hbWUsXG4gICAgICAgICAgYXV0aG9yaXplclR5cGU6IHBheW1lbnQuYXV0aG9yaXplclR5cGUsXG4gICAgICAgICAgZGVzY3JpcHRpb246IHBheW1lbnQuZGVzY3JpcHRpb24sXG4gICAgICAgICAgYXV0aG9yaXplckNvbmZpZ3VyYXRpb246IHBheW1lbnQuYXV0aG9yaXplckNvbmZpZ3VyYXRpb24sXG4gICAgICAgICAgdGFnczogc3BlYy50YWdzLFxuICAgICAgICB9KTtcblxuICAgICAgICBjb25zdCBwcmVmaXggPSBgQUdFTlRDT1JFX1BBWU1FTlRfJHtwYXltZW50Lm5hbWUudG9VcHBlckNhc2UoKS5yZXBsYWNlKC8tL2csICdfJyl9YDtcblxuICAgICAgICAvLyBXaXJlIGVudiB2YXJzIGZyb20gY29uc3RydWN0IG91dHB1dCB0b2tlbnMgaW50byBlbGlnaWJsZSBhZ2VudCBlbnZpcm9ubWVudHMgb25seS5cbiAgICAgICAgLy8gU2VlIGlzUGF5bWVudEVsaWdpYmxlQWdlbnQg4oCUIG5vbi1QeXRob24gb3Igbm9uLUhUVFAgcnVudGltZXMgaGF2ZSBubyBzaGltIHRoYXRcbiAgICAgICAgLy8gY2FuIGNvbnN1bWUgdGhlc2UgZW52IHZhcnMsIGFuZCBnaXZpbmcgdGhlbSBzdHM6QXNzdW1lUm9sZSBvbiB0aGVcbiAgICAgICAgLy8gUHJvY2Vzc1BheW1lbnRSb2xlIHdvdWxkIGJyb2FkZW4gdGhlIHByaXZpbGVnZSBzdXJmYWNlIHVubmVjZXNzYXJpbHkuXG4gICAgICAgIGZvciAoY29uc3QgZW52IG9mIHRoaXMuYXBwbGljYXRpb24uZW52aXJvbm1lbnRzLnZhbHVlcygpKSB7XG4gICAgICAgICAgaWYgKCFpc1BheW1lbnRFbGlnaWJsZUFnZW50KGVudi5hZ2VudCkpIHtcbiAgICAgICAgICAgIGNvbnRpbnVlO1xuICAgICAgICAgIH1cbiAgICAgICAgICBlbnYucnVudGltZS5hZGRFbnZpcm9ubWVudFZhcmlhYmxlKGAke3ByZWZpeH1fTUFOQUdFUl9BUk5gLCBtYW5hZ2VyLnBheW1lbnRNYW5hZ2VyQXJuKTtcbiAgICAgICAgICBlbnYucnVudGltZS5hZGRFbnZpcm9ubWVudFZhcmlhYmxlKGAke3ByZWZpeH1fUFJPQ0VTU19QQVlNRU5UX1JPTEVfQVJOYCwgbWFuYWdlci5wcm9jZXNzUGF5bWVudFJvbGVBcm4pO1xuXG4gICAgICAgICAgLy8gR3JhbnQgcnVudGltZSBleGVjdXRpb24gcm9sZSBwZXJtaXNzaW9uIHRvIGFzc3VtZSB0aGUgUHJvY2Vzc1BheW1lbnRSb2xlLlxuICAgICAgICAgIC8vIFRoZSBQcm9jZXNzUGF5bWVudFJvbGUncyB0cnVzdCBwb2xpY3kgYWxsb3dzIEFjY291bnRSb290UHJpbmNpcGFsLCBidXQgdGhlXG4gICAgICAgICAgLy8gY2FsbGVyIHN0aWxsIG5lZWRzIHN0czpBc3N1bWVSb2xlIG9uIGl0cyBvd24gcm9sZSB0byBwZXJmb3JtIHRoZSBhc3N1bXB0aW9uLlxuICAgICAgICAgIGVudi5ydW50aW1lLnJvbGUuYWRkVG9QcmluY2lwYWxQb2xpY3koXG4gICAgICAgICAgICBuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICAgICAgICAgIGFjdGlvbnM6IFsnc3RzOkFzc3VtZVJvbGUnXSxcbiAgICAgICAgICAgICAgcmVzb3VyY2VzOiBbbWFuYWdlci5wcm9jZXNzUGF5bWVudFJvbGVBcm5dLFxuICAgICAgICAgICAgfSlcbiAgICAgICAgICApO1xuXG4gICAgICAgICAgLy8gR3JhbnQgcGF5bWVudCBkYXRhLXBsYW5lIGFjdGlvbnMgZGlyZWN0bHkgdG8gdGhlIHJ1bnRpbWUgcm9sZS5cbiAgICAgICAgICAvL1xuICAgICAgICAgIC8vIE5PVEU6IFRoaXMgZGV2aWF0ZXMgZnJvbSB0aGUgY2Fub25pY2FsIHJvbGUgbW9kZWwgaW4gdGhlIEFnZW50Q29yZSBQYXltZW50c1xuICAgICAgICAgIC8vIGJldGEgZ3VpZGUsIHdoaWNoIGFzc2lnbnMgR2V0L0xpc3QvQ3JlYXRlIGluc3RydW1lbnQrc2Vzc2lvbiBhY3Rpb25zIHRvIGFcbiAgICAgICAgICAvLyBzZXBhcmF0ZSBNYW5hZ2VtZW50Um9sZSBhbmQgbGltaXRzIHRoZSBhZ2VudCdzIHJvbGUgdG8gUHJvY2Vzc1BheW1lbnQgb25seS5cbiAgICAgICAgICAvLyBUaGUgY3VycmVudCBTREsgcGx1Z2luIChBZ2VudENvcmVQYXltZW50c1BsdWdpbi5nZW5lcmF0ZV9wYXltZW50X2hlYWRlcilcbiAgICAgICAgICAvLyBjYWxscyBHZXRQYXltZW50SW5zdHJ1bWVudCBpbnRlcm5hbGx5IGR1cmluZyB0aGUgNDAyIGF1dG8tcGF5IHBhdGgsIHNvIHRoZVxuICAgICAgICAgIC8vIHJ1bnRpbWUgcm9sZSBuZWVkcyByZWFkIGFjY2Vzcy4gQ3JlYXRlUGF5bWVudFNlc3Npb24gaXMgaW5jbHVkZWQgc29cbiAgICAgICAgICAvLyBgYWdlbnRjb3JlIGludm9rZSAtLWF1dG8tc2Vzc2lvbmAgd29ya3Mgd2l0aG91dCBhIHNlcGFyYXRlIE1hbmFnZW1lbnRSb2xlXG4gICAgICAgICAgLy8gY2FsbC4gVGlnaHRlbiB0aGlzIGlmIHRoZSBTREsgaXMgdXBkYXRlZCB0byBhY2NlcHQgcHJlLWZldGNoZWQgaW5zdHJ1bWVudFxuICAgICAgICAgIC8vIGRldGFpbHMgYW5kIHNwbGl0IGNyZWF0ZS1zZXNzaW9uIGludG8gYSBiYWNrZW5kLW9ubHkgZmxvdy5cbiAgICAgICAgICBlbnYucnVudGltZS5yb2xlLmFkZFRvUHJpbmNpcGFsUG9saWN5KFxuICAgICAgICAgICAgbmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgICAgICAgICBhY3Rpb25zOiBbXG4gICAgICAgICAgICAgICAgJ2JlZHJvY2stYWdlbnRjb3JlOkdldFBheW1lbnRJbnN0cnVtZW50JyxcbiAgICAgICAgICAgICAgICAnYmVkcm9jay1hZ2VudGNvcmU6TGlzdFBheW1lbnRJbnN0cnVtZW50cycsXG4gICAgICAgICAgICAgICAgJ2JlZHJvY2stYWdlbnRjb3JlOkdldFBheW1lbnRJbnN0cnVtZW50QmFsYW5jZScsXG4gICAgICAgICAgICAgICAgJ2JlZHJvY2stYWdlbnRjb3JlOkdldFBheW1lbnRTZXNzaW9uJyxcbiAgICAgICAgICAgICAgICAnYmVkcm9jay1hZ2VudGNvcmU6TGlzdFBheW1lbnRTZXNzaW9ucycsXG4gICAgICAgICAgICAgICAgJ2JlZHJvY2stYWdlbnRjb3JlOkNyZWF0ZVBheW1lbnRTZXNzaW9uJyxcbiAgICAgICAgICAgICAgICAnYmVkcm9jay1hZ2VudGNvcmU6UHJvY2Vzc1BheW1lbnQnLFxuICAgICAgICAgICAgICBdLFxuICAgICAgICAgICAgICByZXNvdXJjZXM6IFttYW5hZ2VyLnBheW1lbnRNYW5hZ2VyQXJuLCBgJHttYW5hZ2VyLnBheW1lbnRNYW5hZ2VyQXJufS8qYF0sXG4gICAgICAgICAgICB9KVxuICAgICAgICAgICk7XG5cbiAgICAgICAgICBpZiAocGF5bWVudC5hdXRvUGF5bWVudCAhPT0gdW5kZWZpbmVkKSB7XG4gICAgICAgICAgICBlbnYucnVudGltZS5hZGRFbnZpcm9ubWVudFZhcmlhYmxlKGAke3ByZWZpeH1fQVVUT19QQVlNRU5UYCwgU3RyaW5nKHBheW1lbnQuYXV0b1BheW1lbnQpKTtcbiAgICAgICAgICB9XG4gICAgICAgICAgaWYgKHBheW1lbnQucGF5bWVudFRvb2xBbGxvd2xpc3QpIHtcbiAgICAgICAgICAgIGVudi5ydW50aW1lLmFkZEVudmlyb25tZW50VmFyaWFibGUoYCR7cHJlZml4fV9UT09MX0FMTE9XTElTVGAsIHBheW1lbnQucGF5bWVudFRvb2xBbGxvd2xpc3Quam9pbignLCcpKTtcbiAgICAgICAgICB9XG4gICAgICAgICAgaWYgKHBheW1lbnQubmV0d29ya1ByZWZlcmVuY2VzKSB7XG4gICAgICAgICAgICBlbnYucnVudGltZS5hZGRFbnZpcm9ubWVudFZhcmlhYmxlKGAke3ByZWZpeH1fTkVUV09SS19QUkVGRVJFTkNFU2AsIHBheW1lbnQubmV0d29ya1ByZWZlcmVuY2VzLmpvaW4oJywnKSk7XG4gICAgICAgICAgfVxuICAgICAgICAgIGlmIChwYXltZW50LmF1dGhvcml6ZXJUeXBlID09PSAnQ1VTVE9NX0pXVCcpIHtcbiAgICAgICAgICAgIGVudi5ydW50aW1lLmFkZEVudmlyb25tZW50VmFyaWFibGUoYCR7cHJlZml4fV9BVVRIX01PREVgLCAnYmVhcmVyJyk7XG4gICAgICAgICAgfVxuICAgICAgICB9XG5cbiAgICAgICAgLy8gQ3JlYXRlIGNvbm5lY3RvcnMgZm9yIHRoaXMgbWFuYWdlclxuICAgICAgICBmb3IgKGNvbnN0IGNvbm5lY3RvciBvZiBwYXltZW50LmNvbm5lY3RvcnMpIHtcbiAgICAgICAgICBjb25zdCBjb25uSWQgPSB0b0Nka0lkKGNvbm5lY3Rvci5uYW1lKTtcbiAgICAgICAgICBjb25zdCBzY2hlbWFDb25uZWN0b3IgPVxuICAgICAgICAgICAgY29ubmVjdG9yLnByb3Zpc2lvbk1vZGUgPT09ICdRVUlDS19DUkVBVEUnXG4gICAgICAgICAgICAgID8gY29ubmVjdG9yXG4gICAgICAgICAgICAgIDoge1xuICAgICAgICAgICAgICAgICAgbmFtZTogY29ubmVjdG9yLm5hbWUsXG4gICAgICAgICAgICAgICAgICBwcm92aWRlcjogY29ubmVjdG9yLnByb3ZpZGVyLFxuICAgICAgICAgICAgICAgICAgLi4uKGNvbm5lY3Rvci5wcm92aXNpb25Nb2RlICYmIHsgcHJvdmlzaW9uTW9kZTogY29ubmVjdG9yLnByb3Zpc2lvbk1vZGUgfSksXG4gICAgICAgICAgICAgICAgICBjcmVkZW50aWFsTmFtZTogY29ubmVjdG9yLmNyZWRlbnRpYWxOYW1lLFxuICAgICAgICAgICAgICAgIH07XG4gICAgICAgICAgY29uc3QgY29tcGF0aWJpbGl0eVByb3BzID0ge1xuICAgICAgICAgICAgcHJvamVjdE5hbWU6IHNwZWMubmFtZSxcbiAgICAgICAgICAgIHBheW1lbnRNYW5hZ2VyOiBtYW5hZ2VyLFxuICAgICAgICAgICAgY29ubmVjdG9yOiBzY2hlbWFDb25uZWN0b3IsXG4gICAgICAgICAgICAvLyBSZW1vdmUgdGhlc2UgbGVnYWN5IG1hbnVhbCBmaWVsZHMgYWZ0ZXIgdGhlIG5ldyBMMyByZWxlYXNlIGlzIHBpbm5lZC5cbiAgICAgICAgICAgIGNvbm5lY3Rvck5hbWU6IGNvbm5lY3Rvci5uYW1lLFxuICAgICAgICAgICAgY29ubmVjdG9yVHlwZTogY29ubmVjdG9yLnByb3ZpZGVyLFxuICAgICAgICAgICAgLi4uKGNvbm5lY3Rvci5wcm92aXNpb25Nb2RlICE9PSAnUVVJQ0tfQ1JFQVRFJyAmJiB7XG4gICAgICAgICAgICAgIGNyZWRlbnRpYWxQcm92aWRlckFybjogY29ubmVjdG9yLmNyZWRlbnRpYWxQcm92aWRlckFybixcbiAgICAgICAgICAgIH0pLFxuICAgICAgICAgIH07XG4gICAgICAgICAgY29uc3QgY29ubiA9IG5ldyBBZ2VudENvcmVQYXltZW50Q29ubmVjdG9yKFxuICAgICAgICAgICAgdGhpcyxcbiAgICAgICAgICAgIGBQYXltZW50JHttZ3JJZH0ke2Nvbm5JZH1gLFxuICAgICAgICAgICAgY29tcGF0aWJpbGl0eVByb3BzIGFzIHVua25vd24gYXMgQ29uc3RydWN0b3JQYXJhbWV0ZXJzPHR5cGVvZiBBZ2VudENvcmVQYXltZW50Q29ubmVjdG9yPlsyXVxuICAgICAgICAgICk7XG5cbiAgICAgICAgICAvLyBXaXJlIGZpcnN0IGNvbm5lY3RvcidzIElEIGFzIGVudiB2YXIgKGVsaWdpYmxlIGFnZW50cyBvbmx5KVxuICAgICAgICAgIGlmIChjb25uZWN0b3IgPT09IHBheW1lbnQuY29ubmVjdG9yc1swXSkge1xuICAgICAgICAgICAgZm9yIChjb25zdCBlbnYgb2YgdGhpcy5hcHBsaWNhdGlvbi5lbnZpcm9ubWVudHMudmFsdWVzKCkpIHtcbiAgICAgICAgICAgICAgaWYgKCFpc1BheW1lbnRFbGlnaWJsZUFnZW50KGVudi5hZ2VudCkpIGNvbnRpbnVlO1xuICAgICAgICAgICAgICBlbnYucnVudGltZS5hZGRFbnZpcm9ubWVudFZhcmlhYmxlKGAke3ByZWZpeH1fQ09OTkVDVE9SX0lEYCwgY29ubi5wYXltZW50Q29ubmVjdG9ySWQpO1xuICAgICAgICAgICAgfVxuICAgICAgICAgIH1cblxuICAgICAgICAgIG5ldyBDZm5PdXRwdXQodGhpcywgYFBheW1lbnQke21ncklkfSR7Y29ubklkfUNvbm5lY3RvcklkYCwge1xuICAgICAgICAgICAgdmFsdWU6IGNvbm4ucGF5bWVudENvbm5lY3RvcklkLFxuICAgICAgICAgIH0pO1xuICAgICAgICAgIGlmIChjb25uZWN0b3IucHJvdmlzaW9uTW9kZSA9PT0gJ1FVSUNLX0NSRUFURScpIHtcbiAgICAgICAgICAgIGNvbnN0IHF1aWNrQ3JlYXRlQ29ubmVjdG9yID0gY29ubiBhcyBBZ2VudENvcmVQYXltZW50Q29ubmVjdG9yICYge1xuICAgICAgICAgICAgICBwYXltZW50Q29ubmVjdG9yU3RhdHVzOiBzdHJpbmc7XG4gICAgICAgICAgICAgIGF1dGhvcml6YXRpb25Vcmw6IHN0cmluZztcbiAgICAgICAgICAgIH07XG4gICAgICAgICAgICBuZXcgQ2ZuT3V0cHV0KHRoaXMsIGBQYXltZW50JHttZ3JJZH0ke2Nvbm5JZH1Db25uZWN0b3JTdGF0dXNgLCB7XG4gICAgICAgICAgICAgIHZhbHVlOiBxdWlja0NyZWF0ZUNvbm5lY3Rvci5wYXltZW50Q29ubmVjdG9yU3RhdHVzLFxuICAgICAgICAgICAgfSk7XG4gICAgICAgICAgICBuZXcgQ2ZuT3V0cHV0KHRoaXMsIGBQYXltZW50JHttZ3JJZH0ke2Nvbm5JZH1BdXRob3JpemF0aW9uVXJsYCwge1xuICAgICAgICAgICAgICB2YWx1ZTogcXVpY2tDcmVhdGVDb25uZWN0b3IuYXV0aG9yaXphdGlvblVybCxcbiAgICAgICAgICAgIH0pO1xuICAgICAgICAgIH1cbiAgICAgICAgfVxuXG4gICAgICAgIC8vIENGTiBPdXRwdXRzIGZvciBwb3N0LWRlcGxveSBzdGF0ZSBwYXJzaW5nXG4gICAgICAgIG5ldyBDZm5PdXRwdXQodGhpcywgYFBheW1lbnQke21ncklkfU1hbmFnZXJBcm5gLCB7XG4gICAgICAgICAgdmFsdWU6IG1hbmFnZXIucGF5bWVudE1hbmFnZXJBcm4sXG4gICAgICAgIH0pO1xuICAgICAgICBuZXcgQ2ZuT3V0cHV0KHRoaXMsIGBQYXltZW50JHttZ3JJZH1NYW5hZ2VySWRgLCB7XG4gICAgICAgICAgdmFsdWU6IG1hbmFnZXIucGF5bWVudE1hbmFnZXJJZCxcbiAgICAgICAgfSk7XG4gICAgICAgIG5ldyBDZm5PdXRwdXQodGhpcywgYFBheW1lbnQke21ncklkfVByb2Nlc3NQYXltZW50Um9sZUFybmAsIHtcbiAgICAgICAgICB2YWx1ZTogbWFuYWdlci5wcm9jZXNzUGF5bWVudFJvbGVBcm4sXG4gICAgICAgIH0pO1xuICAgICAgICBuZXcgQ2ZuT3V0cHV0KHRoaXMsIGBQYXltZW50JHttZ3JJZH1SZXNvdXJjZVJldHJpZXZhbFJvbGVBcm5gLCB7XG4gICAgICAgICAgdmFsdWU6IG1hbmFnZXIucmVzb3VyY2VSZXRyaWV2YWxSb2xlQXJuLFxuICAgICAgICB9KTtcbiAgICAgIH1cbiAgICB9XG5cbiAgICAvLyBTdGFjay1sZXZlbCBvdXRwdXRcbiAgICBuZXcgQ2ZuT3V0cHV0KHRoaXMsICdTdGFja05hbWVPdXRwdXQnLCB7XG4gICAgICBkZXNjcmlwdGlvbjogJ05hbWUgb2YgdGhlIENsb3VkRm9ybWF0aW9uIFN0YWNrJyxcbiAgICAgIHZhbHVlOiB0aGlzLnN0YWNrTmFtZSxcbiAgICB9KTtcbiAgfVxufVxuIl19
#!/usr/bin/env node
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const cdk_stack_1 = require("../lib/cdk-stack");
const agentcore_cdk_1 = require("@aws/agentcore-cdk");
const aws_cdk_lib_1 = require("aws-cdk-lib");
const path = require("path");
const fs = require("fs");
function toEnvironment(target) {
    return {
        account: target.account,
        region: target.region,
    };
}
function sanitize(name) {
    return name.replace(/_/g, '-');
}
function toStackName(projectName, targetName) {
    return `AgentCore-${sanitize(projectName)}-${sanitize(targetName)}`;
}
async function main() {
    // Config root is parent of cdk/ directory. The CLI sets process.cwd() to agentcore/cdk/.
    const configRoot = path.resolve(process.cwd(), '..');
    const configIO = new agentcore_cdk_1.ConfigIO({ baseDir: configRoot });
    const spec = await configIO.readProjectSpec();
    const targets = await configIO.readAWSDeploymentTargets();
    // The vended CDK project compiles against the published @aws/agentcore-cdk
    // schema type, which may lag the CLI's own AgentCoreProjectSpec (e.g. payments,
    // harnesses, gateway fields). Cast once so those fields are reachable.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const specAny = spec;
    // Extract MCP configuration from project spec.
    // Gateway fields are stored in agentcore.json but may not yet be on the
    const mcpSpec = specAny.agentCoreGateways?.length
        ? {
            agentCoreGateways: specAny.agentCoreGateways,
            mcpRuntimeTools: specAny.mcpRuntimeTools,
            unassignedTargets: specAny.unassignedTargets,
        }
        : undefined;
    // Read deployed state for credential ARNs (populated by pre-deploy identity setup)
    let deployedState;
    try {
        deployedState = JSON.parse(fs.readFileSync(path.join(configRoot, '.cli', 'deployed-state.json'), 'utf8'));
    }
    catch {
        // Deployed state may not exist on first deploy
    }
    if (targets.length === 0) {
        throw new Error('No deployment targets configured. Please define targets in agentcore/aws-targets.json');
    }
    // Read harness configs: the full validated spec drives the CFN resource; the
    // role-scoped fields drive the IAM role + container build.
    const projectRoot = path.resolve(configRoot, '..');
    // Read non-S3 KB connector-config files and pass their parsed contents to the
    // L3 verbatim. The L3 does not read files; it expects the parsed
    // connectorParameters keyed by the data source's connectorConfigFile path.
    const connectorParametersByFile = {};
    for (const kb of specAny.knowledgeBases ?? []) {
        for (const ds of kb.dataSources ?? []) {
            if (ds.type !== 'S3' && ds.connectorConfigFile) {
                const abs = path.resolve(projectRoot, ds.connectorConfigFile);
                try {
                    connectorParametersByFile[ds.connectorConfigFile] = JSON.parse(fs.readFileSync(abs, 'utf-8'));
                }
                catch (err) {
                    throw new Error(`Could not read connector config '${ds.connectorConfigFile}' for knowledge base '${kb.name}' at ${abs}: ${err instanceof Error ? err.message : err}`);
                }
            }
        }
    }
    // Synthesize an AWS::BedrockAgentCore::Harness resource for each harness entry in the spec.
    const harnessConfigs = [];
    for (const entry of specAny.harnesses ?? []) {
        const harnessDir = path.resolve(projectRoot, entry.path);
        const harnessPath = path.resolve(harnessDir, 'harness.json');
        try {
            const harnessSpec = agentcore_cdk_1.HarnessSpecSchema.parse(JSON.parse(fs.readFileSync(harnessPath, 'utf-8')));
            harnessConfigs.push({
                name: entry.name,
                executionRoleArn: harnessSpec.executionRoleArn,
                // Only an `existing` memory ref carries a name to wire IAM against; managed memory is
                // owned by the harness (no sibling) and disabled has none — both resolve to undefined.
                memoryName: harnessSpec.memory?.mode === 'existing' ? harnessSpec.memory.name : undefined,
                containerUri: harnessSpec.containerUri,
                hasDockerfile: !!harnessSpec.dockerfile,
                dockerfile: harnessSpec.dockerfile,
                codeLocation: harnessSpec.dockerfile ? harnessDir : undefined,
                tools: harnessSpec.tools,
                skills: harnessSpec.skills,
                apiKeyArn: harnessSpec.model?.apiKeyArn,
                efsAccessPoints: harnessSpec.efsAccessPoints,
                s3AccessPoints: harnessSpec.s3AccessPoints,
                apiFormat: harnessSpec.model?.apiFormat,
                // Full spec + dir drive the AWS::BedrockAgentCore::Harness CFN resource.
                spec: harnessSpec,
                harnessDir,
            });
        }
        catch (err) {
            throw new Error(`Could not read harness.json for "${entry.name}" at ${harnessPath}: ${err instanceof Error ? err.message : err}`);
        }
    }
    const app = new aws_cdk_lib_1.App();
    aws_cdk_lib_1.Tags.of(app).add('hackathon', 'true');
    for (const target of targets) {
        const env = toEnvironment(target);
        const stackName = toStackName(spec.name, target.name);
        // Extract credentials from deployed state for this target
        const targetState = deployedState?.targets;
        const targetResources = targetState?.[target.name]?.resources;
        const credentials = targetResources?.credentials;
        // Payment credential provider ARNs live in the same credentials map as identity credentials
        const paymentCredentials = credentials;
        const paymentSpec = specAny.payments?.length
            ? specAny.payments.map((p) => ({
                name: p.name,
                description: p.description,
                authorizerType: p.authorizerType,
                authorizerConfiguration: p.authorizerConfiguration,
                autoPayment: p.autoPayment,
                paymentToolAllowlist: p.paymentToolAllowlist,
                networkPreferences: p.networkPreferences,
                connectors: p.connectors.map(c => {
                    if (c.provisionMode === 'QUICK_CREATE') {
                        return {
                            name: c.name,
                            provider: 'CoinbaseCDP',
                            provisionMode: 'QUICK_CREATE',
                        };
                    }
                    if (!c.credentialName) {
                        throw new Error(`Manual payment connector "${c.name}" on manager "${p.name}" is missing its credential name.`);
                    }
                    const credentialProviderArn = paymentCredentials?.[c.credentialName]?.credentialProviderArn;
                    if (!credentialProviderArn) {
                        // Fail fast with an actionable message rather than passing an empty
                        // ARN that fails opaquely server-side at CreatePaymentConnector.
                        throw new Error(`Payment connector "${c.name}" on manager "${p.name}" references credential ` +
                            `"${c.credentialName}", but no deployed credential provider was found for it. ` +
                            `Run \`agentcore deploy\` so the credential provider is created first.`);
                    }
                    return {
                        name: c.name,
                        provider: c.provider,
                        ...(c.provisionMode && { provisionMode: c.provisionMode }),
                        credentialName: c.credentialName,
                        credentialProviderArn,
                    };
                }),
            }))
            : undefined;
        new cdk_stack_1.AgentCoreStack(app, stackName, {
            spec,
            mcpSpec,
            credentials,
            connectorParametersByFile,
            harnesses: harnessConfigs.length > 0 ? harnessConfigs : undefined,
            paymentSpec,
            env,
            description: `AgentCore stack for ${spec.name} deployed to ${target.name} (${target.region})`,
            tags: {
                'agentcore:project-name': spec.name,
                'agentcore:target-name': target.name,
            },
        });
    }
    app.synth();
}
main().catch((error) => {
    console.error('AgentCore CDK synthesis failed:', error instanceof Error ? error.message : error);
    process.exit(1);
});
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiY2RrLmpzIiwic291cmNlUm9vdCI6IiIsInNvdXJjZXMiOlsiLi4vLi4vYmluL2Nkay50cyJdLCJuYW1lcyI6W10sIm1hcHBpbmdzIjoiOzs7QUFDQSxnREFBc0U7QUFDdEUsc0RBQTJGO0FBQzNGLDZDQUF5RDtBQUN6RCw2QkFBNkI7QUFDN0IseUJBQXlCO0FBRXpCLFNBQVMsYUFBYSxDQUFDLE1BQTJCO0lBQ2hELE9BQU87UUFDTCxPQUFPLEVBQUUsTUFBTSxDQUFDLE9BQU87UUFDdkIsTUFBTSxFQUFFLE1BQU0sQ0FBQyxNQUFNO0tBQ3RCLENBQUM7QUFDSixDQUFDO0FBRUQsU0FBUyxRQUFRLENBQUMsSUFBWTtJQUM1QixPQUFPLElBQUksQ0FBQyxPQUFPLENBQUMsSUFBSSxFQUFFLEdBQUcsQ0FBQyxDQUFDO0FBQ2pDLENBQUM7QUFFRCxTQUFTLFdBQVcsQ0FBQyxXQUFtQixFQUFFLFVBQWtCO0lBQzFELE9BQU8sYUFBYSxRQUFRLENBQUMsV0FBVyxDQUFDLElBQUksUUFBUSxDQUFDLFVBQVUsQ0FBQyxFQUFFLENBQUM7QUFDdEUsQ0FBQztBQUVELEtBQUssVUFBVSxJQUFJO0lBQ2pCLHlGQUF5RjtJQUN6RixNQUFNLFVBQVUsR0FBRyxJQUFJLENBQUMsT0FBTyxDQUFDLE9BQU8sQ0FBQyxHQUFHLEVBQUUsRUFBRSxJQUFJLENBQUMsQ0FBQztJQUNyRCxNQUFNLFFBQVEsR0FBRyxJQUFJLHdCQUFRLENBQUMsRUFBRSxPQUFPLEVBQUUsVUFBVSxFQUFFLENBQUMsQ0FBQztJQUV2RCxNQUFNLElBQUksR0FBRyxNQUFNLFFBQVEsQ0FBQyxlQUFlLEVBQUUsQ0FBQztJQUM5QyxNQUFNLE9BQU8sR0FBRyxNQUFNLFFBQVEsQ0FBQyx3QkFBd0IsRUFBRSxDQUFDO0lBRTFELDJFQUEyRTtJQUMzRSxnRkFBZ0Y7SUFDaEYsdUVBQXVFO0lBQ3ZFLDhEQUE4RDtJQUM5RCxNQUFNLE9BQU8sR0FBRyxJQUFXLENBQUM7SUFFNUIsK0NBQStDO0lBQy9DLHdFQUF3RTtJQUN4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QUMsY0FBYyxDQUFDLENBQUMsQ0FBQyxTQUFTO1lBQ2pFLFdBQVc7WUFDWCxHQUFHO1lBQ0gsV0FBVyxFQUFFLHVCQUF1QixJQUFJLENBQUMsSUFBSSxnQkFBZ0IsTUFBTSxDQUFDLElBQUksS0FBSyxNQUFNLENBQUMsTUFBTSxHQUFHO1lBQzdGLElBQUksRUFBRTtnQkFDSix3QkFBd0IsRUFBRSxJQUFJLENBQUMsSUFBSTtnQkFDbkMsdUJBQXVCLEVBQUUsTUFBTSxDQUFDLElBQUk7YUFDckM7U0FDRixDQUFDLENBQUM7SUFDTCxDQUFDO0lBRUQsR0FBRyxDQUFDLEtBQUssRUFBRSxDQUFDO0FBQ2QsQ0FBQztBQUVELElBQUksRUFBRSxDQUFDLEtBQUssQ0FBQyxDQUFDLEtBQWMsRUFBRSxFQUFFO0lBQzlCLE9BQU8sQ0FBQyxLQUFLLENBQUMsaUNBQWlDLEVBQUUsS0FBSyxZQUFZLEtBQUssQ0FBQyxDQUFDLENBQUMsS0FBSyxDQUFDLE9BQU8sQ0FBQyxDQUFDLENBQUMsS0FBSyxDQUFDLENBQUM7SUFDakcsT0FBTyxDQUFDLElBQUksQ0FBQyxDQUFDLENBQUMsQ0FBQztBQUNsQixDQUFDLENBQUMsQ0FBQyIsInNvdXJjZXNDb250ZW50IjpbIiMhL3Vzci9iaW4vZW52IG5vZGVcbmltcG9ydCB7IEFnZW50Q29yZVN0YWNrLCB0eXBlIEhhcm5lc3NDb25maWcgfSBmcm9tICcuLi9saWIvY2RrLXN0YWNrJztcbmltcG9ydCB7IENvbmZpZ0lPLCBIYXJuZXNzU3BlY1NjaGVtYSwgdHlwZSBBd3NEZXBsb3ltZW50VGFyZ2V0IH0gZnJvbSAnQGF3cy9hZ2VudGNvcmUtY2RrJztcbmltcG9ydCB7IEFwcCxUYWdzLCB0eXBlIEVudmlyb25tZW50IH0gZnJvbSAnYXdzLWNkay1saWInO1xuaW1wb3J0ICogYXMgcGF0aCBmcm9tICdwYXRoJztcbmltcG9ydCAqIGFzIGZzIGZyb20gJ2ZzJztcblxuZnVuY3Rpb24gdG9FbnZpcm9ubWVudCh0YXJnZXQ6IEF3c0RlcGxveW1lbnRUYXJnZXQpOiBFbnZpcm9ubWVudCB7XG4gIHJldHVybiB7XG4gICAgYWNjb3VudDogdGFyZ2V0LmFjY291bnQsXG4gICAgcmVnaW9uOiB0YXJnZXQucmVnaW9uLFxuICB9O1xufVxuXG5mdW5jdGlvbiBzYW5pdGl6ZShuYW1lOiBzdHJpbmcpOiBzdHJpbmcge1xuICByZXR1cm4gbmFtZS5yZXBsYWNlKC9fL2csICctJyk7XG59XG5cbmZ1bmN0aW9uIHRvU3RhY2tOYW1lKHByb2plY3ROYW1lOiBzdHJpbmcsIHRhcmdldE5hbWU6IHN0cmluZyk6IHN0cmluZyB7XG4gIHJldHVybiBgQWdlbnRDb3JlLSR7c2FuaXRpemUocHJvamVjdE5hbWUpfS0ke3Nhbml0aXplKHRhcmdldE5hbWUpfWA7XG59XG5cbmFzeW5jIGZ1bmN0aW9uIG1haW4oKSB7XG4gIC8vIENvbmZpZyByb290IGlzIHBhcmVudCBvZiBjZGsvIGRpcmVjdG9yeS4gVGhlIENMSSBzZXRzIHByb2Nlc3MuY3dkKCkgdG8gYWdlbnRjb3JlL2Nkay8uXG4gIGNvbnN0IGNvbmZpZ1Jvb3QgPSBwYXRoLnJlc29sdmUocHJvY2Vzcy5jd2QoKSwgJy4uJyk7XG4gIGNvbnN0IGNvbmZpZ0lPID0gbmV3IENvbmZpZ0lPKHsgYmFzZURpcjogY29uZmlnUm9vdCB9KTtcblxuICBjb25zdCBzcGVjID0gYXdhaXQgY29uZmlnSU8ucmVhZFByb2plY3RTcGVjKCk7XG4gIGNvbnN0IHRhcmdldHMgPSBhd2FpdCBjb25maWdJTy5yZWFkQVdTRGVwbG95bWVudFRhcmdldHMoKTtcblxuICAvLyBUaGUgdmVuZGVkIENESyBwcm9qZWN0IGNvbXBpbGVzIGFnYWluc3QgdGhlIHB1Ymxpc2hlZCBAYXdzL2FnZW50Y29yZS1jZGtcbiAgLy8gc2NoZW1hIHR5cGUsIHdoaWNoIG1heSBsYWcgdGhlIENMSSdzIG93biBBZ2VudENvcmVQcm9qZWN0U3BlYyAoZS5nLiBwYXltZW50cyxcbiAgLy8gaGFybmVzc2VzLCBnYXRld2F5IGZpZWxkcykuIENhc3Qgb25jZSBzbyB0aG9zZSBmaWVsZHMgYXJlIHJlYWNoYWJsZS5cbiAgLy8gZXNsaW50LWRpc2FibGUtbmV4dC1saW5lIEB0eXBlc2NyaXB0LWVzbGludC9uby1leHBsaWNpdC1hbnlcbiAgY29uc3Qgc3BlY0FueSA9IHNwZWMgYXMgYW55O1xuXG4gIC8vIEV4dHJhY3QgTUNQIGNvbmZpZ3VyYXRpb24gZnJvbSBwcm9qZWN0IHNwZWMuXG4gIC8vIEdhdGV3YXkgZmllbGRzIGFyZSBzdG9yZWQgaW4gYWdlbnRjb3JlLmpzb24gYnV0IG1heSBub3QgeWV0IGJlIG9uIHRoZVxuICBjb25zdCBtY3BTcGVjID0gc3BlY0FueS5hZ2VudENvcmVHYXRld2F5cz8ubGVuZ3RoXG4gICAgPyB7XG4gICAgICAgIGFnZW50Q29yZUdhdGV3YXlzOiBzcGVjQW55LmFnZW50Q29yZUdhdGV3YXlzLFxuICAgICAgICBtY3BSdW50aW1lVG9vbHM6IHNwZWNBbnkubWNwUnVudGltZVRvb2xzLFxuICAgICAgICB1bmFzc2lnbmVkVGFyZ2V0czogc3BlY0FueS51bmFzc2lnbmVkVGFyZ2V0cyxcbiAgICAgIH1cbiAgICA6IHVuZGVmaW5lZDtcblxuICAvLyBSZWFkIGRlcGxveWVkIHN0YXRlIGZvciBjcmVkZW50aWFsIEFSTnMgKHBvcHVsYXRlZCBieSBwcmUtZGVwbG95IGlkZW50aXR5IHNldHVwKVxuICBsZXQgZGVwbG95ZWRTdGF0ZTogUmVjb3JkPHN0cmluZywgdW5rbm93bj4gfCB1bmRlZmluZWQ7XG4gIHRyeSB7XG4gICAgZGVwbG95ZWRTdGF0ZSA9IEpTT04ucGFyc2UoZnMucmVhZEZpbGVTeW5jKHBhdGguam9pbihjb25maWdSb290LCAnLmNsaScsICdkZXBsb3llZC1zdGF0ZS5qc29uJyksICd1dGY4JykpO1xuICB9IGNhdGNoIHtcbiAgICAvLyBEZXBsb3llZCBzdGF0ZSBtYXkgbm90IGV4aXN0IG9uIGZpcnN0IGRlcGxveVxuICB9XG5cbiAgaWYgKHRhcmdldHMubGVuZ3RoID09PSAwKSB7XG4gICAgdGhyb3cgbmV3IEVycm9yKCdObyBkZXBsb3ltZW50IHRhcmdldHMgY29uZmlndXJlZC4gUGxlYXNlIGRlZmluZSB0YXJnZXRzIGluIGFnZW50Y29yZS9hd3MtdGFyZ2V0cy5qc29uJyk7XG4gIH1cblxuICAvLyBSZWFkIGhhcm5lc3MgY29uZmlnczogdGhlIGZ1bGwgdmFsaWRhdGVkIHNwZWMgZHJpdmVzIHRoZSBDRk4gcmVzb3VyY2U7IHRoZVxuICAvLyByb2xlLXNjb3BlZCBmaWVsZHMgZHJpdmUgdGhlIElBTSByb2xlICsgY29udGFpbmVyIGJ1aWxkLlxuICBjb25zdCBwcm9qZWN0Um9vdCA9IHBhdGgucmVzb2x2ZShjb25maWdSb290LCAnLi4nKTtcblxuICAvLyBSZWFkIG5vbi1TMyBLQiBjb25uZWN0b3ItY29uZmlnIGZpbGVzIGFuZCBwYXNzIHRoZWlyIHBhcnNlZCBjb250ZW50cyB0byB0aGVcbiAgLy8gTDMgdmVyYmF0aW0uIFRoZSBMMyBkb2VzIG5vdCByZWFkIGZpbGVzOyBpdCBleHBlY3RzIHRoZSBwYXJzZWRcbiAgLy8gY29ubmVjdG9yUGFyYW1ldGVycyBrZXllZCBieSB0aGUgZGF0YSBzb3VyY2UncyBjb25uZWN0b3JDb25maWdGaWxlIHBhdGguXG4gIGNvbnN0IGNvbm5lY3RvclBhcmFtZXRlcnNCeUZpbGU6IFJlY29yZDxzdHJpbmcsIFJlY29yZDxzdHJpbmcsIHVua25vd24+PiA9IHt9O1xuICBmb3IgKGNvbnN0IGtiIG9mIHNwZWNBbnkua25vd2xlZGdlQmFzZXMgPz8gW10pIHtcbiAgICBmb3IgKGNvbnN0IGRzIG9mIGtiLmRhdGFTb3VyY2VzID8/IFtdKSB7XG4gICAgICBpZiAoZHMudHlwZSAhPT0gJ1MzJyAmJiBkcy5jb25uZWN0b3JDb25maWdGaWxlKSB7XG4gICAgICAgIGNvbnN0IGFicyA9IHBhdGgucmVzb2x2ZShwcm9qZWN0Um9vdCwgZHMuY29ubmVjdG9yQ29uZmlnRmlsZSk7XG4gICAgICAgIHRyeSB7XG4gICAgICAgICAgY29ubmVjdG9yUGFyYW1ldGVyc0J5RmlsZVtkcy5jb25uZWN0b3JDb25maWdGaWxlXSA9IEpTT04ucGFyc2UoZnMucmVhZEZpbGVTeW5jKGFicywgJ3V0Zi04JykpO1xuICAgICAgICB9IGNhdGNoIChlcnIpIHtcbiAgICAgICAgICB0aHJvdyBuZXcgRXJyb3IoXG4gICAgICAgICAgICBgQ291bGQgbm90IHJlYWQgY29ubmVjdG9yIGNvbmZpZyAnJHtkcy5jb25uZWN0b3JDb25maWdGaWxlfScgZm9yIGtub3dsZWRnZSBiYXNlICcke2tiLm5hbWV9JyBhdCAke2Fic306ICR7ZXJyIGluc3RhbmNlb2YgRXJyb3IgPyBlcnIubWVzc2FnZSA6IGVycn1gXG4gICAgICAgICAgKTtcbiAgICAgICAgfVxuICAgICAgfVxuICAgIH1cbiAgfVxuXG4gIC8vIFN5bnRoZXNpemUgYW4gQVdTOjpCZWRyb2NrQWdlbnRDb3JlOjpIYXJuZXNzIHJlc291cmNlIGZvciBlYWNoIGhhcm5lc3MgZW50cnkgaW4gdGhlIHNwZWMuXG4gIGNvbnN0IGhhcm5lc3NDb25maWdzOiBIYXJuZXNzQ29uZmlnW10gPSBbXTtcbiAgZm9yIChjb25zdCBlbnRyeSBvZiBzcGVjQW55Lmhhcm5lc3NlcyA/PyBbXSkge1xuICAgIGNvbnN0IGhhcm5lc3NEaXIgPSBwYXRoLnJlc29sdmUocHJvamVjdFJvb3QsIGVudHJ5LnBhdGgpO1xuICAgIGNvbnN0IGhhcm5lc3NQYXRoID0gcGF0aC5yZXNvbHZlKGhhcm5lc3NEaXIsICdoYXJuZXNzLmpzb24nKTtcbiAgICB0cnkge1xuICAgICAgY29uc3QgaGFybmVzc1NwZWMgPSBIYXJuZXNzU3BlY1NjaGVtYS5wYXJzZShKU09OLnBhcnNlKGZzLnJlYWRGaWxlU3luYyhoYXJuZXNzUGF0aCwgJ3V0Zi04JykpKTtcbiAgICAgIGhhcm5lc3NDb25maWdzLnB1c2goe1xuICAgICAgICBuYW1lOiBlbnRyeS5uYW1lLFxuICAgICAgICBleGVjdXRpb25Sb2xlQXJuOiBoYXJuZXNzU3BlYy5leGVjdXRpb25Sb2xlQXJuLFxuICAgICAgICAvLyBPbmx5IGFuIGBleGlzdGluZ2AgbWVtb3J5IHJlZiBjYXJyaWVzIGEgbmFtZSB0byB3aXJlIElBTSBhZ2FpbnN0OyBtYW5hZ2VkIG1lbW9yeSBpc1xuICAgICAgICAvLyBvd25lZCBieSB0aGUgaGFybmVzcyAobm8gc2libGluZykgYW5kIGRpc2FibGVkIGhhcyBub25lIOKAlCBib3RoIHJlc29sdmUgdG8gdW5kZWZpbmVkLlxuICAgICAgICBtZW1vcnlOYW1lOiBoYXJuZXNzU3BlYy5tZW1vcnk/Lm1vZGUgPT09ICdleGlzdGluZycgPyBoYXJuZXNzU3BlYy5tZW1vcnkubmFtZSA6IHVuZGVmaW5lZCxcbiAgICAgICAgY29udGFpbmVyVXJpOiBoYXJuZXNzU3BlYy5jb250YWluZXJVcmksXG4gICAgICAgIGhhc0RvY2tlcmZpbGU6ICEhaGFybmVzc1NwZWMuZG9ja2VyZmlsZSxcbiAgICAgICAgZG9ja2VyZmlsZTogaGFybmVzc1NwZWMuZG9ja2VyZmlsZSxcbiAgICAgICAgY29kZUxvY2F0aW9uOiBoYXJuZXNzU3BlYy5kb2NrZXJmaWxlID8gaGFybmVzc0RpciA6IHVuZGVmaW5lZCxcbiAgICAgICAgdG9vbHM6IGhhcm5lc3NTcGVjLnRvb2xzLFxuICAgICAgICBza2lsbHM6IGhhcm5lc3NTcGVjLnNraWxscyxcbiAgICAgICAgYXBpS2V5QXJuOiBoYXJuZXNzU3BlYy5tb2RlbD8uYXBpS2V5QXJuLFxuICAgICAgICBlZnNBY2Nlc3NQb2ludHM6IGhhcm5lc3NTcGVjLmVmc0FjY2Vzc1BvaW50cyxcbiAgICAgICAgczNBY2Nlc3NQb2ludHM6IGhhcm5lc3NTcGVjLnMzQWNjZXNzUG9pbnRzLFxuICAgICAgICBhcGlGb3JtYXQ6IGhhcm5lc3NTcGVjLm1vZGVsPy5hcGlGb3JtYXQsXG4gICAgICAgIC8vIEZ1bGwgc3BlYyArIGRpciBkcml2ZSB0aGUgQVdTOjpCZWRyb2NrQWdlbnRDb3JlOjpIYXJuZXNzIENGTiByZXNvdXJjZS5cbiAgICAgICAgc3BlYzogaGFybmVzc1NwZWMsXG4gICAgICAgIGhhcm5lc3NEaXIsXG4gICAgICB9KTtcbiAgICB9IGNhdGNoIChlcnIpIHtcbiAgICAgIHRocm93IG5ldyBFcnJvcihcbiAgICAgICAgYENvdWxkIG5vdCByZWFkIGhhcm5lc3MuanNvbiBmb3IgXCIke2VudHJ5Lm5hbWV9XCIgYXQgJHtoYXJuZXNzUGF0aH06ICR7ZXJyIGluc3RhbmNlb2YgRXJyb3IgPyBlcnIubWVzc2FnZSA6IGVycn1gXG4gICAgICApO1xuICAgIH1cbiAgfVxuXG4gIGNvbnN0IGFwcCA9IG5ldyBBcHAoKTtcbiAgVGFncy5vZihhcHApLmFkZCgnaGFja2F0aG9uJywgJ3RydWUnKTtcblxuICBmb3IgKGNvbnN0IHRhcmdldCBvZiB0YXJnZXRzKSB7XG4gICAgY29uc3QgZW52ID0gdG9FbnZpcm9ubWVudCh0YXJnZXQpO1xuICAgIGNvbnN0IHN0YWNrTmFtZSA9IHRvU3RhY2tOYW1lKHNwZWMubmFtZSwgdGFyZ2V0Lm5hbWUpO1xuXG4gICAgLy8gRXh0cmFjdCBjcmVkZW50aWFscyBmcm9tIGRlcGxveWVkIHN0YXRlIGZvciB0aGlzIHRhcmdldFxuICAgIGNvbnN0IHRhcmdldFN0YXRlID0gKGRlcGxveWVkU3RhdGUgYXMgUmVjb3JkPHN0cmluZywgdW5rbm93bj4pPy50YXJnZXRzIGFzXG4gICAgICBSZWNvcmQ8c3RyaW5nLCBSZWNvcmQ8c3RyaW5nLCB1bmtub3duPj4gfCB1bmRlZmluZWQ7XG4gICAgY29uc3QgdGFyZ2V0UmVzb3VyY2VzID0gdGFyZ2V0U3RhdGU/Llt0YXJnZXQubmFtZV0/LnJlc291cmNlcyBhcyBSZWNvcmQ8c3RyaW5nLCB1bmtub3duPiB8IHVuZGVmaW5lZDtcbiAgICBjb25zdCBjcmVkZW50aWFscyA9IHRhcmdldFJlc291cmNlcz8uY3JlZGVudGlhbHMgYXNcbiAgICAgIFJlY29yZDxzdHJpbmcsIHsgY3JlZGVudGlhbFByb3ZpZGVyQXJuOiBzdHJpbmc7IGNsaWVudFNlY3JldEFybj86IHN0cmluZyB9PiB8IHVuZGVmaW5lZDtcblxuICAgIC8vIFBheW1lbnQgY3JlZGVudGlhbCBwcm92aWRlciBBUk5zIGxpdmUgaW4gdGhlIHNhbWUgY3JlZGVudGlhbHMgbWFwIGFzIGlkZW50aXR5IGNyZWRlbnRpYWxzXG4gICAgY29uc3QgcGF5bWVudENyZWRlbnRpYWxzID0gY3JlZGVudGlhbHM7XG5cbiAgICBjb25zdCBwYXltZW50U3BlYyA9IHNwZWNBbnkucGF5bWVudHM/Lmxlbmd0aFxuICAgICAgPyBzcGVjQW55LnBheW1lbnRzLm1hcChcbiAgICAgICAgICAocDoge1xuICAgICAgICAgICAgbmFtZTogc3RyaW5nO1xuICAgICAgICAgICAgZGVzY3JpcHRpb24/OiBzdHJpbmc7XG4gICAgICAgICAgICBhdXRob3JpemVyVHlwZTogJ0FXU19JQU0nIHwgJ0NVU1RPTV9KV1QnO1xuICAgICAgICAgICAgYXV0aG9yaXplckNvbmZpZ3VyYXRpb24/OiB1bmtub3duO1xuICAgICAgICAgICAgYXV0b1BheW1lbnQ/OiBib29sZWFuO1xuICAgICAgICAgICAgcGF5bWVudFRvb2xBbGxvd2xpc3Q/OiBzdHJpbmdbXTtcbiAgICAgICAgICAgIG5ldHdvcmtQcmVmZXJlbmNlcz86IHN0cmluZ1tdO1xuICAgICAgICAgICAgY29ubmVjdG9yczoge1xuICAgICAgICAgICAgICBuYW1lOiBzdHJpbmc7XG4gICAgICAgICAgICAgIHByb3ZpZGVyPzogc3RyaW5nO1xuICAgICAgICAgICAgICBwcm92aXNpb25Nb2RlPzogJ01BTlVBTCcgfCAnUVVJQ0tfQ1JFQVRFJztcbiAgICAgICAgICAgICAgY3JlZGVudGlhbE5hbWU/OiBzdHJpbmc7XG4gICAgICAgICAgICB9W107XG4gICAgICAgICAgfSkgPT4gKHtcbiAgICAgICAgICAgIG5hbWU6IHAubmFtZSxcbiAgICAgICAgICAgIGRlc2NyaXB0aW9uOiBwLmRlc2NyaXB0aW9uLFxuICAgICAgICAgICAgYXV0aG9yaXplclR5cGU6IHAuYXV0aG9yaXplclR5cGUsXG4gICAgICAgICAgICBhdXRob3JpemVyQ29uZmlndXJhdGlvbjogcC5hdXRob3JpemVyQ29uZmlndXJhdGlvbixcbiAgICAgICAgICAgIGF1dG9QYXltZW50OiBwLmF1dG9QYXltZW50LFxuICAgICAgICAgICAgcGF5bWVudFRvb2xBbGxvd2xpc3Q6IHAucGF5bWVudFRvb2xBbGxvd2xpc3QsXG4gICAgICAgICAgICBuZXR3b3JrUHJlZmVyZW5jZXM6IHAubmV0d29ya1ByZWZlcmVuY2VzLFxuICAgICAgICAgICAgY29ubmVjdG9yczogcC5jb25uZWN0b3JzLm1hcChjID0+IHtcbiAgICAgICAgICAgICAgaWYgKGMucHJvdmlzaW9uTW9kZSA9PT0gJ1FVSUNLX0NSRUFURScpIHtcbiAgICAgICAgICAgICAgICByZXR1cm4ge1xuICAgICAgICAgICAgICAgICAgbmFtZTogYy5uYW1lLFxuICAgICAgICAgICAgICAgICAgcHJvdmlkZXI6ICdDb2luYmFzZUNEUCcgYXMgY29uc3QsXG4gICAgICAgICAgICAgICAgICBwcm92aXNpb25Nb2RlOiAnUVVJQ0tfQ1JFQVRFJyBhcyBjb25zdCxcbiAgICAgICAgICAgICAgICB9O1xuICAgICAgICAgICAgICB9XG5cbiAgICAgICAgICAgICAgaWYgKCFjLmNyZWRlbnRpYWxOYW1lKSB7XG4gICAgICAgICAgICAgICAgdGhyb3cgbmV3IEVycm9yKFxuICAgICAgICAgICAgICAgICAgYE1hbnVhbCBwYXltZW50IGNvbm5lY3RvciBcIiR7Yy5uYW1lfVwiIG9uIG1hbmFnZXIgXCIke3AubmFtZX1cIiBpcyBtaXNzaW5nIGl0cyBjcmVkZW50aWFsIG5hbWUuYFxuICAgICAgICAgICAgICAgICk7XG4gICAgICAgICAgICAgIH1cbiAgICAgICAgICAgICAgY29uc3QgY3JlZGVudGlhbFByb3ZpZGVyQXJuID0gcGF5bWVudENyZWRlbnRpYWxzPy5bYy5jcmVkZW50aWFsTmFtZV0/LmNyZWRlbnRpYWxQcm92aWRlckFybjtcbiAgICAgICAgICAgICAgaWYgKCFjcmVkZW50aWFsUHJvdmlkZXJBcm4pIHtcbiAgICAgICAgICAgICAgICAvLyBGYWlsIGZhc3Qgd2l0aCBhbiBhY3Rpb25hYmxlIG1lc3NhZ2UgcmF0aGVyIHRoYW4gcGFzc2luZyBhbiBlbXB0eVxuICAgICAgICAgICAgICAgIC8vIEFSTiB0aGF0IGZhaWxzIG9wYXF1ZWx5IHNlcnZlci1zaWRlIGF0IENyZWF0ZVBheW1lbnRDb25uZWN0b3IuXG4gICAgICAgICAgICAgICAgdGhyb3cgbmV3IEVycm9yKFxuICAgICAgICAgICAgICAgICAgYFBheW1lbnQgY29ubmVjdG9yIFwiJHtjLm5hbWV9XCIgb24gbWFuYWdlciBcIiR7cC5uYW1lfVwiIHJlZmVyZW5jZXMgY3JlZGVudGlhbCBgICtcbiAgICAgICAgICAgICAgICAgICAgYFwiJHtjLmNyZWRlbnRpYWxOYW1lfVwiLCBidXQgbm8gZGVwbG95ZWQgY3JlZGVudGlhbCBwcm92aWRlciB3YXMgZm91bmQgZm9yIGl0LiBgICtcbiAgICAgICAgICAgICAgICAgICAgYFJ1biBcXGBhZ2VudGNvcmUgZGVwbG95XFxgIHNvIHRoZSBjcmVkZW50aWFsIHByb3ZpZGVyIGlzIGNyZWF0ZWQgZmlyc3QuYFxuICAgICAgICAgICAgICAgICk7XG4gICAgICAgICAgICAgIH1cbiAgICAgICAgICAgICAgcmV0dXJuIHtcbiAgICAgICAgICAgICAgICBuYW1lOiBjLm5hbWUsXG4gICAgICAgICAgICAgICAgcHJvdmlkZXI6IGMucHJvdmlkZXIsXG4gICAgICAgICAgICAgICAgLi4uKGMucHJvdmlzaW9uTW9kZSAmJiB7IHByb3Zpc2lvbk1vZGU6IGMucHJvdmlzaW9uTW9kZSB9KSxcbiAgICAgICAgICAgICAgICBjcmVkZW50aWFsTmFtZTogYy5jcmVkZW50aWFsTmFtZSxcbiAgICAgICAgICAgICAgICBjcmVkZW50aWFsUHJvdmlkZXJBcm4sXG4gICAgICAgICAgICAgIH07XG4gICAgICAgICAgICB9KSxcbiAgICAgICAgICB9KVxuICAgICAgICApXG4gICAgICA6IHVuZGVmaW5lZDtcblxuICAgIG5ldyBBZ2VudENvcmVTdGFjayhhcHAsIHN0YWNrTmFtZSwge1xuICAgICAgc3BlYyxcbiAgICAgIG1jcFNwZWMsXG4gICAgICBjcmVkZW50aWFscyxcbiAgICAgIGNvbm5lY3RvclBhcmFtZXRlcnNCeUZpbGUsXG4gICAgICBoYXJuZXNzZXM6IGhhcm5lc3NDb25maWdzLmxlbmd0aCA+IDAgPyBoYXJuZXNzQ29uZmlncyA6IHVuZGVmaW5lZCxcbiAgICAgIHBheW1lbnRTcGVjLFxuICAgICAgZW52LFxuICAgICAgZGVzY3JpcHRpb246IGBBZ2VudENvcmUgc3RhY2sgZm9yICR7c3BlYy5uYW1lfSBkZXBsb3llZCB0byAke3RhcmdldC5uYW1lfSAoJHt0YXJnZXQucmVnaW9ufSlgLFxuICAgICAgdGFnczoge1xuICAgICAgICAnYWdlbnRjb3JlOnByb2plY3QtbmFtZSc6IHNwZWMubmFtZSxcbiAgICAgICAgJ2FnZW50Y29yZTp0YXJnZXQtbmFtZSc6IHRhcmdldC5uYW1lLFxuICAgICAgfSxcbiAgICB9KTtcbiAgfVxuXG4gIGFwcC5zeW50aCgpO1xufVxuXG5tYWluKCkuY2F0Y2goKGVycm9yOiB1bmtub3duKSA9PiB7XG4gIGNvbnNvbGUuZXJyb3IoJ0FnZW50Q29yZSBDREsgc3ludGhlc2lzIGZhaWxlZDonLCBlcnJvciBpbnN0YW5jZW9mIEVycm9yID8gZXJyb3IubWVzc2FnZSA6IGVycm9yKTtcbiAgcHJvY2Vzcy5leGl0KDEpO1xufSk7XG4iXX0=
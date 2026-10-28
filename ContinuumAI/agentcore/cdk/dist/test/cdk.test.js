"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const cdk = require("aws-cdk-lib");
const assertions_1 = require("aws-cdk-lib/assertions");
const cdk_stack_1 = require("../lib/cdk-stack");
test('AgentCoreStack synthesizes with empty spec', () => {
    const app = new cdk.App();
    const stack = new cdk_stack_1.AgentCoreStack(app, 'TestStack', {
        spec: {
            name: 'testproject',
            version: 1,
            managedBy: 'CDK',
            runtimes: [],
            memories: [],
            credentials: [],
            evaluators: [],
            onlineEvalConfigs: [],
            configBundles: [],
            policyEngines: [],
            payments: [],
            agentCoreGateways: [],
            mcpRuntimeTools: [],
            unassignedTargets: [],
            datasets: [],
            knowledgeBases: [],
        },
    });
    const template = assertions_1.Template.fromStack(stack);
    template.hasOutput('StackNameOutput', {
        Description: 'Name of the CloudFormation Stack',
    });
});
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiY2RrLnRlc3QuanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyIuLi8uLi90ZXN0L2Nkay50ZXN0LnRzIl0sIm5hbWVzIjpbXSwibWFwcGluZ3MiOiI7O0FBQUEsbUNBQW1DO0FBQ25DLHVEQUFrRDtBQUNsRCxnREFBa0Q7QUFFbEQsSUFBSSxDQUFDLDRDQUE0QyxFQUFFLEdBQUcsRUFBRTtJQUN0RCxNQUFNLEdBQUcsR0FBRyxJQUFJLEdBQUcsQ0FBQyxHQUFHLEVBQUUsQ0FBQztJQUMxQixNQUFNLEtBQUssR0FBRyxJQUFJLDBCQUFjLENBQUMsR0FBRyxFQUFFLFdBQVcsRUFBRTtRQUNqRCxJQUFJLEVBQUU7WUFDSixJQUFJLEVBQUUsYUFBYTtZQUNuQixPQUFPLEVBQUUsQ0FBQztZQUNWLFNBQVMsRUFBRSxLQUFjO1lBQ3pCLFFBQVEsRUFBRSxFQUFFO1lBQ1osUUFBUSxFQUFFLEVBQUU7WUFDWixXQUFXLEVBQUUsRUFBRTtZQUNmLFVBQVUsRUFBRSxFQUFFO1lBQ2QsaUJBQWlCLEVBQUUsRUFBRTtZQUNyQixhQUFhLEVBQUUsRUFBRTtZQUNqQixhQUFhLEVBQUUsRUFBRTtZQUNqQixRQUFRLEVBQUUsRUFBRTtZQUNaLGlCQUFpQixFQUFFLEVBQUU7WUFDckIsZUFBZSxFQUFFLEVBQUU7WUFDbkIsaUJBQWlCLEVBQUUsRUFBRTtZQUNyQixRQUFRLEVBQUUsRUFBRTtZQUNaLGNBQWMsRUFBRSxFQUFFO1NBQ25CO0tBQ0YsQ0FBQyxDQUFDO0lBQ0gsTUFBTSxRQUFRLEdBQUcscUJBQVEsQ0FBQyxTQUFTLENBQUMsS0FBSyxDQUFDLENBQUM7SUFDM0MsUUFBUSxDQUFDLFNBQVMsQ0FBQyxpQkFBaUIsRUFBRTtRQUNwQyxXQUFXLEVBQUUsa0NBQWtDO0tBQ2hELENBQUMsQ0FBQztBQUNMLENBQUMsQ0FBQyxDQUFDIiwic291cmNlc0NvbnRlbnQiOlsiaW1wb3J0ICogYXMgY2RrIGZyb20gJ2F3cy1jZGstbGliJztcbmltcG9ydCB7IFRlbXBsYXRlIH0gZnJvbSAnYXdzLWNkay1saWIvYXNzZXJ0aW9ucyc7XG5pbXBvcnQgeyBBZ2VudENvcmVTdGFjayB9IGZyb20gJy4uL2xpYi9jZGstc3RhY2snO1xuXG50ZXN0KCdBZ2VudENvcmVTdGFjayBzeW50aGVzaXplcyB3aXRoIGVtcHR5IHNwZWMnLCAoKSA9PiB7XG4gIGNvbnN0IGFwcCA9IG5ldyBjZGsuQXBwKCk7XG4gIGNvbnN0IHN0YWNrID0gbmV3IEFnZW50Q29yZVN0YWNrKGFwcCwgJ1Rlc3RTdGFjaycsIHtcbiAgICBzcGVjOiB7XG4gICAgICBuYW1lOiAndGVzdHByb2plY3QnLFxuICAgICAgdmVyc2lvbjogMSxcbiAgICAgIG1hbmFnZWRCeTogJ0NESycgYXMgY29uc3QsXG4gICAgICBydW50aW1lczogW10sXG4gICAgICBtZW1vcmllczogW10sXG4gICAgICBjcmVkZW50aWFsczogW10sXG4gICAgICBldmFsdWF0b3JzOiBbXSxcbiAgICAgIG9ubGluZUV2YWxDb25maWdzOiBbXSxcbiAgICAgIGNvbmZpZ0J1bmRsZXM6IFtdLFxuICAgICAgcG9saWN5RW5naW5lczogW10sXG4gICAgICBwYXltZW50czogW10sXG4gICAgICBhZ2VudENvcmVHYXRld2F5czogW10sXG4gICAgICBtY3BSdW50aW1lVG9vbHM6IFtdLFxuICAgICAgdW5hc3NpZ25lZFRhcmdldHM6IFtdLFxuICAgICAgZGF0YXNldHM6IFtdLFxuICAgICAga25vd2xlZGdlQmFzZXM6IFtdLFxuICAgIH0sXG4gIH0pO1xuICBjb25zdCB0ZW1wbGF0ZSA9IFRlbXBsYXRlLmZyb21TdGFjayhzdGFjayk7XG4gIHRlbXBsYXRlLmhhc091dHB1dCgnU3RhY2tOYW1lT3V0cHV0Jywge1xuICAgIERlc2NyaXB0aW9uOiAnTmFtZSBvZiB0aGUgQ2xvdWRGb3JtYXRpb24gU3RhY2snLFxuICB9KTtcbn0pO1xuIl19
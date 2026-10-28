#!/usr/bin/env node
import { App, Tags } from 'aws-cdk-lib';
import { DataStack } from '../lib/data-stack';
import { PortalStack } from '../lib/portal-stack';
import { SchedulerStack } from '../lib/scheduler-stack';

const app = new App();
const ctx = (key: string): string => app.node.tryGetContext(key) ?? '';
const list = (key: string): string[] => ctx(key).split(',').map((s) => s.trim()).filter(Boolean);

// us-east-1, next to the Aurora cluster, the agent runtime and the WhatsApp account.
const env = { account: ctx('account'), region: ctx('region') };

const data = new DataStack(app, 'TrialTalk-Data', { env });
const scheduler = new SchedulerStack(app, 'TrialTalk-Scheduler', {
  env,
  data,
  whatsappPhoneNumberId: ctx('whatsappPhoneNumberId'),
  agentCoreMemoryId: ctx('agentCoreMemoryId'),
});
new PortalStack(app, 'TrialTalk-Portal', {
  env,
  data,
  scheduler,
  whatsappPhoneNumberId: ctx('whatsappPhoneNumberId'),
  legacyTable: ctx('legacyTable'),
  approvedTemplates: ctx('approvedTemplates'),
  loginTemplate: ctx('loginTemplate'),
  alertFrom: ctx('alertFrom'),
  alertTo: ctx('alertTo'),
  staffCallbackUrls: list('staffCallbackUrls'),
  corsOrigins: list('corsOrigins'),
  cognitoDomainPrefix: ctx('cognitoDomainPrefix'),
  otlpEndpoint: ctx('otlpEndpoint') || undefined,
  otelLayerArn: ctx('otelLayerArn') || undefined,
});

Tags.of(app).add('project', 'trialtalk');
Tags.of(app).add('data-classification', 'synthetic-phi');

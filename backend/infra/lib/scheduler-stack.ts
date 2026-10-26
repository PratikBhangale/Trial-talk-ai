import { Stack, StackProps } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import { Construct } from 'constructs';
import { DataStack } from './data-stack';
import { PyFunction } from './py-function';

export interface SchedulerStackProps extends StackProps {
  data: DataStack;
  whatsappPhoneNumberId: string;
  agentCoreMemoryId?: string;
}

/**
 * scheduler: schedule group for rem-<reminder_id>, the role Scheduler assumes, and the reminder
 * Lambda (approved WhatsApp template, no agent).
 */
export class SchedulerStack extends Stack {
  readonly groupName = 'trialtalk-reminders';
  readonly reminderFn: PyFunction;
  readonly schedulerRole: iam.Role;

  constructor(scope: Construct, id: string, props: SchedulerStackProps) {
    super(scope, id, props);

    new scheduler.CfnScheduleGroup(this, 'Group', { name: this.groupName });

    this.reminderFn = new PyFunction(this, 'ReminderFn', {
      handler: 'reminder.handler.handler',
      data: props.data,
      login: 'messaging_api',
      environment: {
        WHATSAPP_PHONE_NUMBER_ID: props.whatsappPhoneNumberId,
        AGENTCORE_MEMORY_ID: props.agentCoreMemoryId ?? '',
      },
    });
    this.reminderFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['social-messaging:SendWhatsAppMessage'],
        resources: [`arn:aws:social-messaging:${this.region}:${this.account}:phone-number-id/*`],
      }),
    );
    if (props.agentCoreMemoryId) {
      this.reminderFn.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ['bedrock-agentcore:CreateEvent'],
          resources: [`arn:aws:bedrock-agentcore:${this.region}:${this.account}:memory/${props.agentCoreMemoryId}`],
        }),
      );
    }

    this.schedulerRole = new iam.Role(this, 'SchedulerRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com', {
        conditions: { StringEquals: { 'aws:SourceAccount': this.account } },
      }),
      description: 'EventBridge Scheduler invoking the TrialTalk reminder Lambda',
    });
    this.reminderFn.grantInvoke(this.schedulerRole);
  }
}

# Continuum-ai

```
agent/                 AgentCore trial agent (agent/agentcore, agent/trial_agent)
db/                    Aurora schema, roles/RLS, seed, DB tests
coordinator-frontend/  Staff and participant web pages (HTML/JS/CSS)
coordinator-backend/   Staff and participant API Lambdas, deploy scripts, tests, local dev server
triggers/              Event-driven Lambdas (ingest, router, reminder, reconcile, whatsapp) and their deploy script
```

Source of truth is what is deployed in AWS (account 180026596905, us-east-1). `agent/trial_agent`, `triggers/` and `coordinator-backend/lambdas` match the deployed code. The original design doc is `TrialTalk build plan.pdf` (kept out of git).

# Alert Templates

This folder provides baseline alert rules for `P1-17`.

File:
- `prometheus-rules.example.yml`

The rule file is a template. Metric names may need mapping to your exporters.

## Required Alert Coverage

- Redis pending event backlog
- Missing worker heartbeats (`orchestrator`, `dispatcher`, `scheduler`)
- Central queue backlog over SLA
- Abnormal growth of scheduler rollback/escalation actions
- Frontend WebSocket disconnect spikes

## Recommended Rollout

1. Map template metric names to your real names.
2. Deploy rules in `warning` mode first.
3. Tune thresholds by real traffic baseline for 1-2 weeks.
4. Promote critical rules to paging policies.


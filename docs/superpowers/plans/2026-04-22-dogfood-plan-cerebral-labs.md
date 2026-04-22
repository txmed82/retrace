# Retrace Dogfooding Plan for Cerebral Labs

## Goal
Validate that Retrace reliably finds real, fixable UX and engineering issues from Cerebral Labs session data before wider rollout.

## Success Criteria
- At least 3 weekly runs produce findings engineers agree are real bugs.
- At least 30% of high-severity findings are converted into tickets.
- At least 20% of generated tickets are shipped fixes within the pilot window.
- False-positive rate stays below 25% after detector tuning.
- Median time from report generation to triage decision is under 24 hours.

## Scope (Pilot)
- One PostHog project with active production traffic.
- One product surface with measurable user journeys (signup, onboarding, checkout, etc.).
- One engineering lead + one PM/ops owner for triage.

## Preconditions
- PostHog personal API key in `.env` (`RETRACE_POSTHOG_API_KEY`).
- `config.yaml` checked with correct `posthog.host`, `project_id`, and detector toggles.
- LLM endpoint configured and reachable (`RETRACE_LLM_API_KEY` if required by provider).
- Report path (`./reports`) and data path (`./data`) writable in the runtime environment.

## Rollout Plan

### Week 0: Environment + Baseline
- Run `retrace init` (or manually verify config) against Cerebral Labs PostHog project.
- Run `retrace doctor` and fix all failures.
- Execute one baseline run: `retrace run`.
- Review one generated report end-to-end with engineering + PM.
- Record baseline metrics: total findings, severity mix, obvious false positives.

### Week 1: Controlled Dogfood Cadence
- Run Retrace on a fixed cadence (every 6 hours via cron or `docker compose`).
- Triage reports daily in a 15-minute async/sync review.
- For each finding: classify as `real bug`, `known issue`, `false positive`, or `needs more evidence`.
- Create tickets for `real bug` findings and link source report/session URLs.

### Week 2: Tune for Signal Quality
- Disable or adjust noisy detectors in `config.yaml`.
- Adjust `lookback_hours`, `max_sessions_per_run`, and cluster thresholds for cleaner clustering.
- Compare pre/post tuning metrics (false positive rate, ticket conversion rate).
- Keep one changelog entry per config change and its observed impact.

### Week 3: Team Workflow Integration
- Make report review part of weekly engineering planning.
- Define owner rotation for daily triage.
- Add a lightweight template for ticket handoff:
  - affected_count
  - severity
  - reproduced? (yes/no)
  - likely owner
  - target fix date

### Week 4: Go/No-Go Decision
- Review 4-week metrics against success criteria.
- Decide one:
  - Go: expand to more Cerebral Labs product surfaces.
  - Hold: run another 2-week tuning sprint.
  - No-go: park until detector/LLM quality gaps are addressed.

## Metrics Dashboard (Track Weekly)
- Runs completed / scheduled runs.
- Findings by severity (`critical`, `high`, `medium`, `low`).
- Findings -> tickets conversion rate.
- Ticket -> shipped fix conversion rate.
- False-positive rate.
- Time-to-triage and time-to-fix.

## Operating Rhythm
- Daily: triage latest report.
- Weekly: review detector performance and config changes.
- Biweekly: review impact with product + engineering leadership.

## Risks and Mitigations
- Noisy findings: tighten detector toggles and cluster thresholds.
- Low trust from engineers: require explicit false-positive labeling and publish weekly precision trend.
- LLM instability: pin model, log prompt/response metadata, and keep deterministic detector outputs as source of truth.
- Operational drift: keep `retrace doctor` in CI or scheduled checks.

## Immediate Next Actions
1. Add Cerebral Labs PostHog `project_id` and LLM endpoint to `config.yaml`.
2. Run `retrace doctor` and resolve failures.
3. Run first pilot report (`retrace run`) and schedule first triage review.
4. Create a shared tracker for findings classification and ticket outcomes.

# Usage

Retrace is a CLI-first tool. Commands are grouped by concern.

## Core workflow

```bash
retrace quickstart                           # zero-config setup
retrace qa list                              # view open incidents
retrace qa show INC-XXXXXX                   # inspect one incident
retrace qa reproduce INC-XXXXXX             # generate and run a regression spec
retrace qa fix INC-XXXXXX --repo org/repo  # score repo, create fix prompt, open draft PR
retrace qa auto --repo org/repo             # full pipeline: reproduce + fix PR for top incident
```

## Incident sources

```bash
retrace run                                  # ingest PostHog sessions and detect issues
retrace api serve                            # run the replay/error ingest API
retrace api process-replays               # finalize queued replay batches
retrace digest                               # markdown rollup of activity
```

## Tester

```bash
retrace tester create --name "Signup" --mode describe --prompt "Go through signup and verify dashboard loads" --app-url http://127.0.0.1:3000
retrace tester run <spec_id>
retrace tester baseline accept <spec_id> --run-dir data/ui-tests/runs/<run>
retrace tester api-create --name "Health check" --method GET --url http://127.0.0.1:3000/api/health --expected-status 200
```

## Review

```bash
retrace review --pr https://github.com/org/repo/pull/42 --llm --post-comment
```

## Monitor

```bash
retrace monitor route add --name prod-slack --kind slack --url https://hooks.slack.com/services/...
retrace monitor route test prod-slack
retrace monitor route list
```

## Repair

```bash
retrace repair list
retrace repair run <task_id>
retrace repair verify <task_id>
```

## Admin

```bash
retrace doctor                               # run health checks
retrace --version                            # show version
```

## Command groups

| Group | Purpose |
|-------|---------|
| `qa` | Incident lifecycle: list, show, reproduce, fix, auto |
| `tester` | UI and API test specs, runs, baselines, suites |
| `review` | PR diff analysis |
| `monitor` | Alert routes, rules, dispatch |
| `repair` | Agent-driven fix runner |
| `api` | Ingest server, replay processing, SDK keys, source maps |
| `digest` | Daily markdown report |

## Full reference

For detailed command flags and examples, see the in-app help (`retrace <command> --help`) or the architecture docs.

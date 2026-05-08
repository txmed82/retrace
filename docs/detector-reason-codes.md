# Detector Reason Codes

Retrace detector signals include:

- `confidence`: one of `low`, `medium`, or `high`.
- `reason_codes`: one or more stable strings explaining why the detector fired.

Reason codes are designed for issue summaries, evidence timelines, and future false-positive tuning. Additive codes are allowed; existing codes should not be renamed without a migration note.

## Built-In Codes

| Detector | Confidence | Reason code | Meaning |
| --- | --- | --- | --- |
| `network_5xx` | `high` | `network_5xx.status_5xx` | A captured network event returned an HTTP 5xx status. |
| `network_4xx` | `medium` | `network_4xx.status_4xx` | A captured network event returned an actionable HTTP 4xx status. |
| `console_error` | `medium` | `console_error.error_level` | A captured console event used an error or assert level. |
| `blank_render` | `high` | `blank_render.low_node_count_after_dwell` | A page stayed below the DOM node threshold after the dwell window. |
| `error_toast` | `medium` | `error_toast.error_like_dom_added` | A newly added DOM node looked like an error alert or toast. |
| `dead_click` | `medium` | `dead_click.no_followup_dom_or_network` | A click had no nearby DOM mutation or network activity. |
| `rage_click` | `medium` | `rage_click.repeated_same_target` | The same target was clicked repeatedly inside the detector window. |
| `session_abandon_on_error` | `medium` | `session_abandon.error_near_session_end` | An error-like event occurred shortly before the replay ended. |

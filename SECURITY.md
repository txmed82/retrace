# Security Policy

Retrace handles replay data, metadata, service tokens, and optional third-party
API keys. Treat security and privacy issues as high priority.

## Supported Versions

Security fixes target the current `master` branch until versioned releases are
published.

## Reporting a Vulnerability

Please do not open a public issue for vulnerabilities.

Report security concerns by emailing the maintainer listed on the GitHub
repository profile, or by opening a private GitHub security advisory if that is
available for the repository.

Include:

- affected command, API endpoint, SDK version, or deployment mode
- reproduction steps
- impact assessment
- whether replay data, API keys, service tokens, or prompt artifacts are exposed
- any logs or screenshots with secrets removed

## Secrets and Replay Data

- Do not commit `.env`, SDK secrets, service tokens, PostHog keys, LLM keys,
  Linear keys, or GitHub tokens.
- Public browser SDK keys are write-only and should not grant replay read access.
- Service tokens should be scoped to the minimum required permissions.
- Prompt artifacts must not include secrets.
- Replay capture should default to masked inputs and support block/mask
  selectors for sensitive regions.

## Safe Disclosure Expectations

We aim to acknowledge valid reports promptly and coordinate fixes before public
details are shared. If the issue affects replay privacy, token scope, prompt
injection, or unauthorized replay access, please allow time for a patch and
release notes before disclosure.

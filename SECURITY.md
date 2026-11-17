# Security policy

## Supported versions

Only the latest release on the default branch receives security fixes.

## Reporting a vulnerability

Do **not** open a public issue for a security problem. Contact the
maintainer privately (see the repository's contact information once it
is published) and include:

- a description of the issue and its impact;
- steps to reproduce, if any;
- any suggested fix, if you have one.

You will receive a response as soon as the report has been reviewed.
Please do not disclose the issue publicly until a fix is available.

## What counts as sensitive

- OAuth tokens, API keys, passwords, private keys, verification codes.
- Credential files (`siwc_tokens.json`, `.env`, anything under
  `~/.muse-gpt-bridge/`).
- Server addresses, tunnel domains, and internal network details.
- Personal data of any user of the software.

## Handling credentials

- The bridge reads `BRIDGE_TOKEN` from an environment variable only.
  It is never written to a file by the bridge itself.
- SIWC credentials live only in the user's local credential file and
  are never uploaded, logged, or included in any report.
- If you believe a credential has leaked, revoke it first (in the
  ChatGPT settings, disconnect the connected application; rotate the
  bridge token), then report.

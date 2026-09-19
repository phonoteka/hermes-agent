# Bitrix24 webhook contract

This document is the developer/operator contract for the bundled `bitrix24` plugin webhook surface.

## Scope

- **Outbound Hermes -> Bitrix24** uses `BITRIX24_WEBHOOK_URL` and supports only the old REST webhook base shape:
  - `https://{portal}/rest/{user}/{secret}`
- **REST v3 is unsupported and fail-closed**:
  - `/rest/api/{user}/{token}`
- **Inbound Bitrix24 -> Hermes** uses the dedicated gateway route `POST /bitrix24/webhook`.
- The dedicated route is active only when `platforms.webhook.extra.bitrix24.enabled: true`.

## Route boundary

Bitrix24 outgoing webhooks do not use Hermes generic webhook signatures.

- Dedicated Bitrix auth is payload auth.application_token + auth.domain.
- The route does not use Hermes generic HMAC headers.
- Generic /webhooks/<name> remains HMAC/Svix/GitLab-token gated.
- Do **not** plug Bitrix24 outgoing webhook directly into generic /webhooks/<name> unless an external relay signs Hermes HMAC.

## Payload contract

The dedicated route/parser accepts:

- `application/json`
- `application/x-www-form-urlencoded`

Retry-stable dedup/event IDs may come from the explicit `event_id` field or be derived from `event/event_handler_id/material`.

## Sensitive data handling

The following values are sensitive and must stay redacted from public outputs, prompts, logs, and operator-facing artifacts:

- `BITRIX24_WEBHOOK_URL`
- `BITRIX24_APPLICATION_TOKEN`
- `auth.application_token`
- `DOWNLOAD_URL`
- raw download links

## Minimal config example

Use placeholders only:

```yaml
plugins:
  enabled:
    - bitrix24

platforms:
  webhook:
    enabled: true
    extra:
      host: 127.0.0.1
      port: 8644
      routes:
        github-example:
          secret: ${HERMES_WEBHOOK_SECRET}
          prompt: "GitHub event: {event}"
          deliver: log
      bitrix24:
        enabled: true
        route_path: /bitrix24/webhook
        deliver: log
```

Environment secrets:

```text
BITRIX24_WEBHOOK_URL=https://{portal}/rest/{user}/{secret}
BITRIX24_APPLICATION_TOKEN=<bitrix24-outgoing-webhook-application-token>
```

## Operational notes

- Keep the Bitrix24 incoming webhook base at the old REST shape; do not trim it to `/rest/api/...` and do not append a method suffix when storing `BITRIX24_WEBHOOK_URL`.
- The dedicated Bitrix route exists because the generic webhook adapter is intentionally provider-neutral and signature-gated.
- If you need to traverse the generic route for some reason, place a relay in front of Hermes that validates Bitrix and then adds Hermes-compatible signing.

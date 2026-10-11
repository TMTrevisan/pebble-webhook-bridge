# Pebble webhook bridge

## Overview

Receives Pebble Index ring voice transcriptions via webhook and queues them as JSON files. A Muse assistant polls `/queue/pending`, processes the transcriptions, answers in chat, and acknowledges processed items. No answer-back to the ring is needed. Python uses only the standard library.

## Quick start

```sh
cp .env.example .env
# Edit .env and set WEBHOOK_TOKEN to a long random token.
docker compose up -d --build
```

Transcriptions persist in `./queue` on the host. The server binds to `0.0.0.0`; `PORT` defaults to `8787`, and `QUEUE_DIR` defaults to `./queue` outside Docker. `WEBHOOK_TOKEN` is required; startup fails if it is missing or empty.

## Pebble app setup

- Open **Index 01 Settings > Webhook**.
- Set the gesture to **Hold & Talk**.
- Set the URL to `https://<your-domain>/webhook/`.
- Set **What to send** to **Transcription only**.
- If supported, optionally add `Authorization: Bearer <same token>`. The webhook does not require or validate this header.

## NGINX reverse proxy example

Inside an HTTPS `server` block, replace `<synology-ip>` with the Docker host's address:

```nginx
location /webhook/ {
    # Preserve the receiver endpoint when proxy_pass replaces the prefix.
    rewrite ^/webhook/$ /webhook break;
    proxy_pass http://<synology-ip>:8787/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

This exposes webhook ingestion. Muse can access `/queue/*` over the private network on port 8787; if remote polling is needed, configure an HTTPS proxy for `/queue/` that preserves the path and Authorization header.

## API reference

Queue endpoints require `Authorization: Bearer <WEBHOOK_TOKEN>` and return `401` for missing or invalid credentials. Responses are JSON.

| Method and endpoint | Request | Response |
| --- | --- | --- |
| `POST /webhook` (also `/webhook/`) | JSON `{"text":"..."}`, URL-encoded or multipart field `transcription`, `text`, or `transcript`, or `text/plain` | `200 {"ok":true,"id":"..."}`; empty text or malformed input returns `400` |
| `GET /queue/pending` | Bearer header | `200 {"pending":[{"id":"...","text":"...","received_at":"..."}]}` sorted oldest first |
| `POST /queue/ack` | Bearer header, JSON `{"id":"..."}` | Deletes item; `200 {"ok":true}` or `404 {"error":"not found"}` |
| `DELETE /queue/<id>` | Bearer header; ID in path | Same deletion behavior as acknowledgment |
| `GET /health` | No auth | `200 {"ok":true,"pending":0}` with current count |

IDs have the format `<unix_timestamp>-<8 lowercase hex characters>`, without the `.json` suffix. Queue files contain `id`, `text`, and `received_at` (ISO UTC). Acknowledgments accept only filenames matching `^[0-9]+-[0-9a-f]{8}\.json$`, preventing path traversal. Polling does not remove items; acknowledge after processing.

## Security notes

Bearer authentication applies only to `/queue/*`, using a constant-time comparison. `/webhook` is open by design because the Pebble app may not support custom auth headers well; the optional Authorization header above is accepted but not enforced. `/health` is also public.

Use a long random token, keep `.env` private, and use HTTPS for external access. Anyone who can reach the webhook can enqueue text. Restrict port 8787 to trusted networks and apply proxy access controls or rate limits if needed. Queue files contain the transcription text, so protect the host directory.

## Troubleshooting

Inspect startup errors and timestamped request logs:

```sh
docker logs pebble-webhook-bridge
docker compose logs -f webhook
```

Test locally (replace the token and ID):

```sh
curl http://localhost:8787/health
curl -X POST http://localhost:8787/webhook/ \
  -H 'Content-Type: application/json' -d '{"text":"Remember to buy milk"}'
curl -X POST http://localhost:8787/webhook -d 'transcription=Hello+Muse'
curl -X POST http://localhost:8787/webhook -F 'transcript=Hello Muse'
curl -X POST http://localhost:8787/webhook \
  -H 'Content-Type: text/plain' --data 'Hello Muse'
curl http://localhost:8787/queue/pending -H 'Authorization: Bearer YOUR_TOKEN'
curl -X POST http://localhost:8787/queue/ack \
  -H 'Authorization: Bearer YOUR_TOKEN' -H 'Content-Type: application/json' \
  -d '{"id":"1700000000-0123abcd"}'
curl -X DELETE http://localhost:8787/queue/1700000000-0123abcd \
  -H 'Authorization: Bearer YOUR_TOKEN'
```

For `401`, ensure Muse uses the exact token from `.env`, then recreate the container after token changes with `docker compose up -d --force-recreate`. For `400`, check the body format and ensure the transcription is nonempty. For `404` on acknowledgment, check the ID or whether it was already deleted. If webhook delivery fails, check DNS, HTTPS, firewall rules, and the proxy route; both `/webhook` and `/webhook/` work directly on the receiver.

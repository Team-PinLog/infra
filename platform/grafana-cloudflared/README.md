# Grafana dedicated Cloudflare Tunnel

This component runs a dedicated connector in `monitoring` and sends requests directly to the Grafana ClusterIP. It does not reuse any existing connector or token.

## Authoritative Cloudflare Public Hostname contract

- Hostname: `monitoring.pin-log.com`
- Path: blank
- Type: `HTTP`
- URL: `kube-prometheus-stack-grafana.monitoring.svc.cluster.local:80`
- No additional origin overrides are configured.

Grafana's canonical root URL is `https://monitoring.pin-log.com`. The retained SSAFY ingress host and `/grafana` manifest avoids recreating the route, but with the current URL settings it is not an instant live fallback.

## Credential handoff gate

The connector reads only `/etc/cloudflared/token`, mounted from Secret `monitoring/grafana-cloudflared-token`, exact key `token`. The token owner must create the strict-scoped SealedSecret at `secrets/monitoring/grafana-cloudflared-token.sealedsecret.yaml` through a hidden stdin handoff. Never put a token, plaintext Secret, placeholder, or generated ciphertext in this component directory or in review output.

Do not merge until the Public Hostname and tunnel exist, the strict-scoped SealedSecret name/namespace/key/template structure is reviewed, and CI passes. The Public Hostname is confirmed externally; the connector and SealedSecret remain activation gates.

## Availability and rollback

The single connector uses `Recreate`, so an update has a short interruption while the old Pod exits and the new Pod becomes Ready. Rollback requires a reviewed Git revert restoring the old root_url and `serve_from_sub_path=true`, verified through GitOps, before disabling the connector and Public Hostname. Preserve the existing Grafana ingress and its host/path throughout rollback; its retention avoids recreating the route, but it is not an instant live fallback. Credential retirement is a separately approved action.

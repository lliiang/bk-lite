# NATS Executor

NATS Executor is a cross-platform executor for local commands, remote SSH commands, and remote file transfer.

## Runtime Configuration

| Environment variable | Required | Description |
| --- | --- | --- |
| `NATS_URLS` | Yes | NATS server URLs. Use a `tls:` URL to enable TLS config rendering in `support-files/startup.sh`. |
| `NATS_INSTANCE_ID` | Yes | Executor instance ID used for NATS subscriptions. |
| `NATS_CA_FILE` | Required for TLS | CA file used when `NATS_URLS` starts with `tls:`. |
| `SSH_KNOWN_HOSTS_FILE` | No | Enables SSH/SCP host key verification when set to a known_hosts file path. |
| `LOG_LEVEL` | No | Log level written to stdout: `debug`, `info`, `warn`, `error`. Defaults to `info`. Read once at process start. |

## Logging

Logs are structured `log/slog` text records written to stdout. In the container image
`support-files/service.conf` redirects supervisor output to `/dev/stdout`, so
`docker logs <container>` already carries the full stream; no log file has to be collected.

The level comes from `LOG_LEVEL` when the process starts (`logger/logger.go`), and
`debug`, `info`, `warn`/`warning`, `error` are accepted; any other value falls back to
`info`. There is no runtime switch, so change the variable and restart the process.

```bash
# binary on a host (started by collector-sidecar with --config)
LOG_LEVEL=debug /opt/fusion-collectors/bin/nats-executor --config /opt/fusion-collectors/generated/<config>.yml

# regional container: the compose file passes LOG_LEVEL through with an info default
cd /opt/bk-lite/proxy
LOG_LEVEL=debug docker compose up -d nats-executor
docker logs -f "$(docker compose ps -q nats-executor)"
```

`/opt/bk-lite/proxy` is the default install path (`install_path` in the deployment payload).
Restore the default afterwards with `LOG_LEVEL=info docker compose up -d nats-executor`,
or put `LOG_LEVEL=debug` in the generated `.env` to keep it across restarts. Startup logs
the effective level, which is the quickest way to confirm the change took effect:

```text
time=2026-10-21T10:00:00.000Z level=INFO msg="Waiting for messages... (log level: debug)"
```

The record itself is `INFO`; the parenthesised value is the effective level.

Every record carries `Instance: <instance_id>` (the zone name for the regional container,
the node id for host deployments), so concurrent requests stay distinguishable. What
`debug` adds on top of `info`/`warn`/`error`:

- NATS traffic: `[SSH Subscribe] Received message, size: N bytes`, `Response sent successfully`.
- SSH handshake: `Using public key authentication`, `Password authentication enabled`,
  `SSH connection established successfully`, `SSH connection closed`.
- Command lifecycle: `Executing command...`, `Command executed successfully in <duration>`,
  `Output length: N bytes`. Success logs only the byte count, never the payload.
- Failure detail: `Command execution failed after <duration> - Error: ...` followed by
  `Output: <captured output>`. This is the only place the full captured output is logged.
- Local execution: `Starting command execution`, `Command: <redacted>`, `Timeout: Ns`,
  `Command execution failed after <duration>, exit code: N`, `Full output: <captured output>`.
  `sshpass` passwords are redacted before logging.
- Health check: `[Health Check] Received health check request from subject: ...`.

Failures are still visible at the default level through `warn`/`error` records, for example
`[SSH Execute] Command execution failed after ... - Error: ...` or
`[SCP] Instance: ... failure | cause=... | next=...`, which names the most likely cause and
the next check.

When a request fails to produce any log line, the failure is upstream of this process:
verify that the subject matches `{action}.{location}.{instance_id}` and that
`NATS_INSTANCE_ID` equals the caller's target id (zone name for the regional container).

## Execution Response Contract

Requests use the envelope `{"args": [{...}], "kwargs": {}}`; execution parameters live in
`args[0]`. `local.execute.<instance_id>` and `ssh.execute.<instance_id>` reply with the
following JSON document:

| Field | Type | Meaning |
| --- | --- | --- |
| `result` | string | Captured `stdout + stderr`. On failure it can hold partial command output instead of an error message. |
| `instance_id` | string | Instance id that served the request. |
| `success` | bool | Whether the command exited successfully within its timeout. |
| `code` | string, optional | `invalid_request`, `timeout`, `dependency_failure`, `execution_failure`. Set only on failure (SSH also sets it for connection-stage failures). |
| `error` | string, optional | Failure summary. Callers must read this field: `result` may be empty or only partial output. |
| `stage` | string, optional | SSH only: `tcp_connect`, `ssh_dial`, `legacy_retry`, `session_create`, `command_run`. |
| `category` | string, optional | SSH only: `network`, `compatibility`, `auth`, `dependency`, `remote_timeout`, `remote_exit`. |

`stage` and `category` let a caller tell "the executor could not reach the target"
(`tcp_connect`/`network`, `ssh_dial`/`auth`) apart from "the target ran the command and it
failed" (`command_run`/`remote_exit`).

Output is bounded: stdout and stderr share a 1 MiB budget
(`utils/shared_output_capture.go`). On overflow the result ends with
`...[output truncated]` and a `warn` record reports the dropped byte counts. Stderr is
appended after stdout, so ordering between the two streams is not preserved in `result`.

For live output, set `stream_logs: true` together with `stream_log_topic` and
`execution_id`. Line events `{"execution_id","stream","line","timestamp"}` are published
to that topic while the command runs, and `result` still carries the complete capture at
the end. Job execution streams to `job.stream.<execution_id>.<node>`.

`health.check.<instance_id>` replies with
`{"success": true, "status": "ok", "instance_id": "...", "timestamp": "<RFC3339>"}` and
does not run any command.

Callers read `result` on success and `error` (falling back to `result`) on failure; see
`agents/stargazer/plugins/script_executor.py` for the collection path.

## SSH Host Key Verification

By default, SSH execution and SCP transfer keep the historical compatibility behavior and do not verify remote host identity. Set `SSH_KNOWN_HOSTS_FILE` to enable strict host key verification for both paths.

```bash
SSH_KNOWN_HOSTS_FILE=/etc/nats-executor/known_hosts
```

When this variable is set:

- SSH execution uses the configured `known_hosts` file through Go's `knownhosts` verifier.
- SCP upload and download use `StrictHostKeyChecking=yes` and `UserKnownHostsFile=<path>`.
- Modern and legacy SSH compatibility retries both keep host key verification enabled.
- Connections fail when the target host is missing from the file or its key no longer matches.

Create or refresh the file with trusted host keys before enabling the variable:

```bash
mkdir -p /etc/nats-executor
ssh-keyscan -H -p 22 10.0.0.8 >> /etc/nats-executor/known_hosts
chmod 0644 /etc/nats-executor/known_hosts
```

Container example:

```bash
docker run \
  -e NATS_URLS="nats://admin:password@nats:4222" \
  -e NATS_INSTANCE_ID="executor-1" \
  -e SSH_KNOWN_HOSTS_FILE="/etc/nats-executor/known_hosts" \
  -v /etc/nats-executor/known_hosts:/etc/nats-executor/known_hosts:ro \
  bklite/nats-executor
```

Leave `SSH_KNOWN_HOSTS_FILE` unset if the deployment has not prepared trusted host keys yet. This preserves the previous compatibility behavior.

## Testing

```bash
go test ./ssh -count=1
```

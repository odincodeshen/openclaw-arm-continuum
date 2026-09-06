# Sanitized Queue Worker Design Package

This synthetic package is intentionally flawed. It contains no private source
code, credentials, hostnames, or customer data. Use it only to validate the
bounded engineering review workflow introduced in v1.3.

## Review request

Review this design package and identify implementation and architecture risks,
missing tests, and release-readiness concerns.

## Architecture

The API writes a job into an in-memory list and immediately returns the list
index as the job ID. Two worker threads poll the list and process pending jobs.
The process is deployed as two independent containers behind a load balancer.

Completed results remain in process memory for later status requests. A client
retries job creation whenever it does not receive a response within two
seconds. There is no idempotency key. Deployments terminate existing
containers immediately after the new container passes an HTTP liveness check.

## Implementation excerpt

```python
jobs = []


def create_job(payload):
    job = {"status": "pending", "payload": payload, "attempts": 0}
    jobs.append(job)
    return len(jobs) - 1


def run_next(handler):
    for job in jobs:
        if job["status"] == "pending":
            job["status"] = "running"
            job["attempts"] += 1
            result = handler(job["payload"])
            job["result"] = result
            job["status"] = "complete"
            return True
    return False
```

## Current tests

- `create_job()` returns zero for the first job.
- `run_next()` returns false when the list is empty.
- One happy-path handler result is stored in the job.

There are no concurrency, retry, exception, restart, multi-container, malformed
payload, authorization, load, or deployment-drain tests.

## Sanitized failure log

```text
12:00:01 container-a create_job returned job_id=41
12:00:03 client timeout; retrying create request
12:00:03 container-b create_job returned job_id=12
12:00:05 status request job_id=41 returned 404
12:01:00 container-a terminated during deployment
```

## Constraints

- The public API contract cannot change during this release.
- At-least-once client retries must be supported.
- A job must remain queryable across process restarts.
- The first production release is expected to run two or more containers.

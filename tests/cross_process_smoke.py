"""One-shot ERP/Ecom cross-process Connect smoke harness.

Run directly with ``python tests/cross_process_smoke.py`` once the sibling Ecom
repository contains ``src/connect-smoke.ts``,
``src/connected-checkout-smoke.ts``, and ``src/connected-order-smoke.ts``.
It starts one temporary ERP loopback server, runs the read-only Ecom read smoke,
then runs the Ecom connected checkout smoke with ``ATLAS_ERP_ALLOW_WRITE=1``
against the same server, and finally the lower-level Ecom order smoke.  The
checkout smoke is the primary write path: it needs Ecom's own database, so it
runs only when ``ATLAS_ECOM_DATABASE_URL`` is set and is reported as skipped
otherwise.  It is intentionally not named ``test_*.py`` so normal unittest
discovery does not start a server or Node.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from http.client import HTTPConnection
from pathlib import Path
from typing import cast

ERP_ROOT = Path(__file__).resolve().parents[1]
ECOM_ROOT = ERP_ROOT.parent / "atlas-ecom"
ECOM_CLIENT = ECOM_ROOT / "src" / "connect-smoke.ts"
ECOM_CHECKOUT_CLIENT = ECOM_ROOT / "src" / "connected-checkout-smoke.ts"
ECOM_ORDER_CLIENT = ECOM_ROOT / "src" / "connected-order-smoke.ts"
# The transport takes one token per peer app_id; the Ecom clients still take a
# single token under the retired name.  The harness bridges the two, which is
# the whole of the credential rename's blast radius: until the sibling reads
# ATLAS_ERP_PEER_TOKENS itself, this file is the only place that knows both
# names, and the Ecom clients keep working only because the same token value is
# written into each.
PEER_TOKENS_ENV = "ATLAS_ERP_PEER_TOKENS"
ECOM_CLIENT_TOKEN_ENV = "ATLAS_ERP_CONNECT_TOKEN"
PEER_APP_ID = "atlas-ecom"
HOST_ENV = "ATLAS_ERP_CONNECT_HOST"
PORT_ENV = "ATLAS_ERP_CONNECT_PORT"
ALLOW_WRITE_ENV = "ATLAS_ERP_ALLOW_WRITE"
ECOM_DATABASE_ENV = "ATLAS_ECOM_DATABASE_URL"
# The server binds its socket only after every configured store has opened and
# created its schema, so this budget covers the protocol store's DDL as well as
# the two product ones.  Ten seconds was measured against two stores and was
# tight enough to fail a cold run; thirty costs nothing when startup is fast.
HEALTH_TIMEOUT_SECONDS = 30.0
CLIENT_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 5.0


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_health(
    process: subprocess.Popen[str], host: str, port: int
) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"ERP server exited with status {return_code}")
        connection = HTTPConnection(host, port, timeout=0.5)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            body = response.read()
            if response.status == 200:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and payload.get("status") == "ok":
                    return
        except OSError:
            pass
        finally:
            connection.close()
        time.sleep(0.05)
    raise TimeoutError("timed out waiting for ERP /health")


def _stop_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        if process.poll() is None:
            process.terminate()
    except OSError:
        pass
    try:
        # communicate() drains the pipes and waits for/reaps the child.
        return process.communicate(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=STOP_TIMEOUT_SECONDS)


def _client_result(stdout: str) -> dict[str, object] | None:
    """Return the JSON object a client printed last, or None if it printed none.

    Every client ends with one JSON line describing what it proved.  It is read
    back rather than only printed so a caller can assert on a real ``sale_id``
    or cursor instead of re-deriving it, and the parse is of the client's own
    output and nothing else.
    """

    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return cast("dict[str, object]", payload)
    return None


def _run_client(
    script: Path,
    url: str,
    token: str,
    *,
    extra_env: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, object] | None]:
    if not script.is_file():
        raise FileNotFoundError(f"Ecom client not found: {script}")

    client_env = os.environ.copy()
    client_env["ATLAS_ERP_URL"] = url
    client_env[ECOM_CLIENT_TOKEN_ENV] = token
    client_env.update(extra_env or {})
    result = subprocess.run(
        ["node", "--experimental-strip-types", script.relative_to(ECOM_ROOT).as_posix()],
        cwd=ECOM_ROOT,
        env=client_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CLIENT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode, _client_result(result.stdout)


def run_smoke() -> dict[str, dict[str, object]]:
    """Run every client and return each one's result keyed by client name.

    The results are what the smoke proved, so they are returned as well as
    printed: a caller that has to show a reader which sale id and which audit
    cursor were involved should quote the client rather than guess.
    """

    host = "127.0.0.1"
    port = _free_loopback_port()
    token = secrets.token_urlsafe(32)
    server_env = os.environ.copy()
    server_env.update(
        {
            PEER_TOKENS_ENV: json.dumps({PEER_APP_ID: token}),
            HOST_ENV: host,
            PORT_ENV: str(port),
        }
    )
    url = f"http://{host}:{port}"
    # The connected checkout writes to Ecom's own database, so it runs only
    # when that database is named.  The value is never printed.
    ecom_database = os.environ.get(ECOM_DATABASE_ENV, "").strip()
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "atlas_erp.connect_server"],
            cwd=ERP_ROOT,
            env=server_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _wait_for_health(process, host, port)
        # The read smoke runs first so it sees the pre-order state.  Then the
        # guarded connected checkout posts one durable order and replays it, and
        # the lower-level order smoke posts one plain manual sale.  Each client
        # inherits the environment, so the database URL reaches the checkout
        # smoke without being echoed anywhere.
        clients: tuple[tuple[Path, Mapping[str, str] | None, bool], ...] = (
            (ECOM_CLIENT, None, False),
            (ECOM_CHECKOUT_CLIENT, {ALLOW_WRITE_ENV: "1"}, ecom_database == ""),
            (ECOM_ORDER_CLIENT, {ALLOW_WRITE_ENV: "1"}, False),
        )
        results: dict[str, dict[str, object]] = {}
        for script, extra_env, skip_without_database in clients:
            if skip_without_database:
                print(f"skipping {script.name}: {ECOM_DATABASE_ENV} is not set")
                continue
            return_code, payload = _run_client(script, url, token, extra_env=extra_env)
            if return_code != 0:
                raise RuntimeError(
                    f"Ecom client {script.name} exited with status {return_code}"
                )
            results[script.stem] = payload or {}
        return results
    finally:
        if process is not None:
            _stop_process(process)


def main() -> int:
    try:
        run_smoke()
    except Exception as exc:
        print(f"cross-process smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

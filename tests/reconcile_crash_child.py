"""A child process that dies inside the window a reconciliation has to close.

A connected sale is two writes in two databases: the master's ``accepted``
decision in the protocol store, and the sale in the owner's own database.  There
is no transaction over both, so a process can be lost between them, and the state
that leaves behind is a decision with no effect.  This script produces exactly
that state from a real process, so the reconciliation can be tested against it
rather than against a hand-built one.

It is :func:`~atlas_erp.connect_server.main`'s own startup sequence -- the same
store builders, the same :func:`~atlas_erp.connect_server._startup_business`, the
same grants -- with one thing changed and one thing added:

* the business store is a subclass that stops at its first ``save_sale`` and
  stands still, so the durable write for a posted sale never happens.  A subclass
  rather than a wrapper because the store has a dozen methods to delegate and
  exactly one to stop.
* a daemon thread posts one connected sale to the child's own listener.  The
  request is answered inside that stop, so the peer never sees an answer, the
  idempotency key is never aborted, and nothing is rolled back -- which is what a
  process that is killed mid-request leaves behind, and why the parent kills it
  rather than asking it to stop.

The stop is where the window is: the ``accepted`` decision is already committed
to the protocol store when the durable write is reached, and the sale exists only
in memory that is about to be lost.  The parent can see the committed decision
in the database, which is how it knows the child is inside the window rather than
merely starting up.

Run it as ``python tests/reconcile_crash_child.py <item_id> <sale_id>`` with the
connection strings and the peer token in the environment, exactly as
``cross_process_smoke`` supplies them.  It is intentionally not named
``test_*.py``: discovery must not start a server or talk to a database.

The hold is bounded rather than infinite, so this process ends by itself even if
the parent that was meant to kill it never arrives.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.client import HTTPConnection

from atlas_erp.business import JournalEntry, Sale
from atlas_erp.business_store import PostgresBusinessStore
from atlas_erp.connect_server import (
    DATABASE_ENV,
    DEFAULT_HOST,
    HOST_ENV,
    PORT_ENV,
    ConnectServer,
    _database_store,
    _environment_peer_tokens,
    _proposal_store,
    _startup_business,
    grant_configured_peers,
)

# How long this child will stand in the window if nothing kills it.  Long enough
# that a parent polling a database will never lose the race, short enough that a
# forgotten child is gone rather than lingering.
CRASH_HOLD_SECONDS = 60.0


class _StoppedWriteStore(PostgresBusinessStore):
    """The owner's store, stopped at the first durable write for a posted sale."""

    def __init__(self, dsn: str, reached: threading.Event) -> None:
        self._reached = reached
        super().__init__(dsn)

    def save_sale(self, sale: Sale, journal: JournalEntry) -> None:
        # Reached strictly after the master accepted: the decision is committed
        # in the protocol store before the transport writes the sale through, and
        # this is the write that follows it.
        self._reached.set()
        threading.Event().wait(CRASH_HOLD_SECONDS)
        super().save_sale(sale, journal)


def _post_sale(
    host: str, port: int, token: str, item_id: str, sale_id: str
) -> None:
    """Post one connected sale to this process's own listener.

    The response is never read: the request is answered inside the store's stop,
    so this thread is waiting on a socket nobody will write to when the process
    is killed.  A refused connection is the ordinary ending, not a failure worth
    reporting from a process that is about to die anyway.
    """

    body = json.dumps(
        {
            "customer_id": "customer-1",
            "sale_id": sale_id,
            "lines": [
                {"item_id": item_id, "quantity": 1, "unit_price_cents": 1250}
            ],
        }
    ).encode("utf-8")
    connection = HTTPConnection(host, port, timeout=CRASH_HOLD_SECONDS)
    try:
        connection.request(
            "POST",
            "/connect/sales",
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        connection.getresponse()
    except OSError:
        return
    finally:
        connection.close()


def main() -> int:
    item_id, sale_id = sys.argv[1], sys.argv[2]
    host = os.environ.get(HOST_ENV, DEFAULT_HOST)
    port = int(os.environ[PORT_ENV])
    peer_tokens = _environment_peer_tokens()
    # The test configures exactly one peer, so this is the token the request below
    # has to present, and there is no case to enumerate.
    token = next(iter(peer_tokens.values()))
    reached = threading.Event()
    # One store, stopped: the fixture is already written by the test that started
    # this child, so the only durable write this process attempts is the one it
    # is going to lose.  The proposal store is this protocol's own, opened as the
    # adapter opens it.
    business_store = _StoppedWriteStore(os.environ[DATABASE_ENV], reached)
    business, protocol = _startup_business(business_store, _proposal_store())
    grant_configured_peers(protocol)
    server = ConnectServer(
        business,
        protocol,
        peer_tokens,
        host=host,
        port=port,
        sale_store=_database_store(),
        business_store=business_store,
    )
    server.start()
    threading.Thread(
        target=_post_sale,
        args=(host, port, token, item_id, sale_id),
        daemon=True,
        name="atlas-erp-crash-post",
    ).start()
    # One line, and only one, so a parent that never sees the window can say what
    # this process did manage to do.
    print(
        "crash-window"
        if reached.wait(timeout=CRASH_HOLD_SECONDS)
        else "never reached the durable write",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

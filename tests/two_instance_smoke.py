"""Three-instance ERP/Ecom/Connect smoke that proves the databases are separate.

``tests/cross_process_smoke.py`` runs the real Ecom clients against a real ERP
loopback server, but all the databases can be in one throwaway server there, and
that is consistent with the products sharing a database: nothing in that run
would notice.  This script removes the ambiguity by giving each database its own
``postgres:16-alpine`` container, on its own host port, holding only its own
database, and then asserting the separation rather than assuming it.

There are **three** containers, not two.  The third holds ``atlas_connect``: the
protocol module's own durable store, the proposal record.  It gets a container of
its own rather than a second database on one of the product's servers because the
claim it carries -- that the protocol store is not a peer's database -- is only
as strong as its weakest link: a second database on the ERP server would make
"each product owns exactly one database" false, and an ERP superuser would be one
query away from every peer's decisions.  The cost is one more container in
a script that already had two.

Run it directly with ``python tests/two_instance_smoke.py``.  It provisions,
checks, runs the smoke and the database-backed proposal-store tests, and tears
every container down again whether it passed or failed.  It is intentionally not
named ``test_*.py``: normal unittest discovery must not start Docker or Node.

The checks, in the order they run, and what each one is an assertion about:

* ``instances_are_separate`` -- the three host ports differ and each instance
  holds exactly one of the three databases, so there is no single server that
  could answer for two of them.
* ``peer_credentials_are_refused`` -- every one of the six accounts is refused by
  each of the other two servers, so no product holds a database login on the
  peer's server, and neither holds one on the protocol store.
* ``cross_database_reference_is_refused`` -- the same ``SELECT`` text that reads
  the other instance's table is accepted on that instance's own connection and
  refused by the instance asking.  The positive control is what makes this an
  attempt rather than a formality: the refusal is about the database boundary,
  not about the SQL.
* ``each_product_sees_only_its_own_tables`` -- each product connection sees every
  table its own product declares, and none of the tables either of the other
  instances actually holds, read from the live catalog rather than from
  configuration.  This is also where "neither product's instance holds the
  protocol's table" is answered.
* ``the_sale_travelled_through_the_protocol`` -- the sale the Ecom client
  submitted is in the ERP instance, the order that submitted it is in the Ecom
  instance, the decision that authorised it is in the protocol instance, each
  side's only knowledge of the other is an id the protocol carried, and no
  instance holds another's tables at all.
* ``the_protocol_store_holds_no_product_state`` -- the protocol instance holds
  the proposal table and none of the product tables, read from its live catalog.
* ``peer_rows_are_confined_to_their_own_peer`` -- a real login as one peer
  cannot read or update another peer's proposal, in both directions, with a
  positive control on its own row so the zero is the row policy and not a
  statement written wrong.

The passwords are disposable, are set only in this process's environment and in
the containers, and are never printed: every line below is built from
``Instance.shape`` or ``PeerCredential.shape``, which cannot contain one.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg import sql

# The harness lives beside this script and is imported rather than reimplemented.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cross_process_smoke  # noqa: E402
from atlas_erp.business_store import CREATE_SCHEMA_SQL  # noqa: E402
from atlas_erp.connect_server import (  # noqa: E402
    CONNECT_DATABASE_ENV,
    DATABASE_ENV as ERP_DATABASE_ENV,
)
from atlas_erp.proposal_store import (  # noqa: E402
    CREATE_PROPOSALS_SQL,
    PostgresProposalStore,
)
from atlas_erp.sale_store import CREATE_COMMANDS_SQL  # noqa: E402

ERP_ROOT = Path(__file__).resolve().parents[1]
IMAGE = "postgres:16-alpine"
ERP_DATABASE = "atlas_erp"
ECOM_DATABASE = "atlas_ecom"
# The protocol module's own database.  It is the third one, and it is not a
# product's: see the module docstring for why it gets a server of its own.
CONNECT_DATABASE = "atlas_connect"
# Every container this script creates is labelled, so a run that was killed hard
# enough to skip the teardown leaves something findable:
# docker ps -a --filter label=atlas-erp.isolated
ISOLATION_LABEL = "atlas-erp.isolated"
# A first run has to pull the image, so this one gets longer than the rest.
IMAGE_TIMEOUT_SECONDS = 300
DOCKER_TIMEOUT_SECONDS = 60
READY_TIMEOUT_SECONDS = 90
# The store tests this script runs against the instances it just provisioned.
# Raised from 180s when the reconciler's restart proof joined that file: it starts
# real child processes against these same instances, so the file now does work
# that is measured in process startups rather than in queries.  The budget only
# matters when a case hangs.
STORE_TEST_TIMEOUT_SECONDS = 300
# The three databases, as the isolation checks name them.
ISOLATED_DATABASES = (ERP_DATABASE, ECOM_DATABASE, CONNECT_DATABASE)
# The peer whose real login is aimed at another peer's row, and a second peer so
# the row-level proof has two directions.  ``atlas-hq`` is a real product id and
# is *not* connected in this run: the only thing that exists for it is one
# proposal row the owner writes through the store's own API, which is what the
# adapter does when a peer proposes something.
CONNECTED_PEER = "atlas-ecom"
SECOND_PEER = "atlas-hq"
# What PostgreSQL says when it refuses a reference to another database.  Matched
# on purpose: any other refusal means the statement was wrong, not that the
# boundary held.
CROSS_DATABASE_REFUSAL = "cross-database references are not implemented"



def _declared_tables(*statements: str) -> tuple[str, ...]:
    """Return the tables a product's own DDL declares.

    Read from the product's DDL rather than listed here, so a table added to
    either store is a table the isolation checks look for without anyone
    remembering to update a hand-written list.
    """

    return tuple(
        re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", "\n".join(statements))
    )


# What the ERP product itself creates in its own database.
ERP_TABLES = _declared_tables(*CREATE_SCHEMA_SQL, CREATE_COMMANDS_SQL)
# What the protocol module creates in its own database.
CONNECT_TABLES = _declared_tables(*CREATE_PROPOSALS_SQL)


@dataclass(frozen=True)
class Instance:
    """One throwaway container holding one database."""

    name: str
    user: str
    database: str
    port: int
    # Never in a repr, so a traceback that quotes an Instance cannot print it.
    password: str = field(repr=False)

    def _dsn(self, port: int, database: str) -> str:
        # ``token_urlsafe`` is already URL-safe, so the password needs no
        # escaping and this string is the only place one is ever assembled.
        return f"postgresql://{self.user}:{self.password}@127.0.0.1:{port}/{database}"

    @property
    def dsn(self) -> str:
        """This account against this instance.  Never print it."""
        return self._dsn(self.port, self.database)

    def credentials_on(self, port: int, database: str) -> str:
        """This account aimed at another instance: the cross-access attempt."""

        return self._dsn(port, database)

    @property
    def shape(self) -> str:
        """The connection string with the password removed, safe to print."""

        return f"postgresql://{self.user}:<redacted>@127.0.0.1:{self.port}/{self.database}"


@dataclass(frozen=True)
class PeerCredential:
    """A peer's own login on the protocol store.

    The role name **is** the peer's ``app_id``, because the store's row policy
    is written as ``peer_id = current_user`` and a role cannot change which role
    it is.  A peer's ``app_id`` contains a dash, so the role is quoted; nothing
    else about the identifier is special, and a peer's login is confined to its
    own rows without this script telling the database which peers exist.
    """

    peer_id: str
    password: str = field(repr=False)

    @property
    def role(self) -> str:
        """The role as SQL names it: quoted, because an app_id has a dash in it."""

        return f'"{self.peer_id}"'

    def dsn(self, connect: Instance) -> str:
        """This peer's own login on the protocol instance.  Never print it.

        The role name goes in unquoted here: libpq sends the user name verbatim as
        a startup parameter, and the server matches it against the role
        :attr:`role` created.
        """

        return (
            f"postgresql://{self.peer_id}:{self.password}"
            f"@127.0.0.1:{connect.port}/{connect.database}"
        )

    @property
    def shape(self) -> str:
        """The connection string with the password removed, safe to print."""

        return f"postgresql://{self.peer_id}:<redacted>@{CONNECT_DATABASE}"


@dataclass(frozen=True)
class IsolatedInstances:
    """The three instances, held separately on purpose."""

    erp: Instance
    ecom: Instance
    connect: Instance

    @property
    def all(self) -> tuple[Instance, ...]:
        return (self.erp, self.ecom, self.connect)

    def by_database(self, database: str) -> Instance:
        for instance in self.all:
            if instance.database == database:
                return instance
        raise KeyError(database)


def _scrub(text: str, *hidden: str) -> str:
    """Remove every disposable password from text bound for an error message."""

    for secret in hidden:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _docker(
    *args: str,
    timeout: int = DOCKER_TIMEOUT_SECONDS,
    check: bool = True,
    hide: tuple[str, ...] = (),
) -> str:
    """Run one Docker command to completion and return its stdout."""

    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        # No argument is echoed: ``-e POSTGRES_PASSWORD=...`` is one of them.
        detail = _scrub(result.stderr.strip(), *hide)
        raise RuntimeError(f"docker {args[0]} failed ({result.returncode}): {detail}")
    return _scrub(result.stdout.strip(), *hide)


def _free_loopback_ports(count: int) -> tuple[int, ...]:
    """Return ``count`` loopback ports, free now and different from each other.

    Every socket is bound before any is released, so a later port cannot be
    handed out an earlier one: "the ports differ" is true by construction rather
    than by luck.
    """

    with ExitStack() as stack:
        sockets = [
            stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            for _ in range(count)
        ]
        for listener in sockets:
            listener.bind(("127.0.0.1", 0))
        return tuple(int(listener.getsockname()[1]) for listener in sockets)


def _start_container(instance: Instance, run_id: str) -> str:
    """Start one container for one product and return its container id."""

    container_id = _docker(
        "run",
        "-d",
        "--name",
        instance.name,
        "--label",
        f"{ISOLATION_LABEL}={run_id}",
        "-e",
        f"POSTGRES_USER={instance.user}",
        "-e",
        f"POSTGRES_DB={instance.database}",
        "-e",
        f"POSTGRES_PASSWORD={instance.password}",
        "-p",
        f"127.0.0.1:{instance.port}:5432",
        IMAGE,
        timeout=IMAGE_TIMEOUT_SECONDS,
        hide=(instance.password,),
    )
    return container_id


def _wait_ready(instance: Instance) -> None:
    """Poll the instance until it accepts a connection, or give up."""

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    reason = "no attempt completed"
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(instance.dsn, connect_timeout=2):
                return
        except psycopg.Error as error:
            reason = _scrub(str(error).strip().splitlines()[0], instance.password)
            time.sleep(0.25)
    raise TimeoutError(f"{instance.name} was not ready in {READY_TIMEOUT_SECONDS}s: {reason}")


def _remove_container(name: str, container_id: str) -> None:
    """Remove one container this run created, and nothing else.

    The name is looked up first and its id compared with the id ``docker run``
    returned.  A different id means the name was reused by something else, and
    the removal is refused rather than performed on a stranger.
    """

    current = _docker("inspect", "--format", "{{.Id}}", name, check=False)
    if current == "":
        print(f"{name} was already gone", flush=True)
        return
    if current != container_id:
        raise RuntimeError(
            f"refusing to remove {name}: it is not the container this run created"
        )
    _docker("rm", "-f", name)
    print(f"removed {name}", flush=True)


@contextmanager
def isolated_instances() -> Iterator[IsolatedInstances]:
    """Provision one container per database, and remove all three whatever happens."""

    run_id = secrets.token_hex(4)
    erp_port, ecom_port, connect_port = _free_loopback_ports(3)
    started: list[tuple[str, str]] = []
    try:
        erp = Instance(
            name=f"atlas-erp-isolated-{run_id}",
            user="atlas_erp",
            database=ERP_DATABASE,
            port=erp_port,
            password=secrets.token_urlsafe(24),
        )
        ecom = Instance(
            name=f"atlas-ecom-isolated-{run_id}",
            user="atlas_ecom",
            database=ECOM_DATABASE,
            port=ecom_port,
            password=secrets.token_urlsafe(24),
        )
        connect = Instance(
            name=f"atlas-connect-isolated-{run_id}",
            user="atlas_connect",
            database=CONNECT_DATABASE,
            port=connect_port,
            password=secrets.token_urlsafe(24),
        )
        # Started one at a time and recorded as each starts, so a failure
        # half way through is still cleaned up by the teardown below.
        for instance in (erp, ecom, connect):
            started.append((instance.name, _start_container(instance, run_id)))
        instances = IsolatedInstances(erp=erp, ecom=ecom, connect=connect)
        for instance in instances.all:
            _wait_ready(instance)
        yield instances
    finally:
        for name, container_id in reversed(started):
            _remove_container(name, container_id)


@contextmanager
def _database_environment(instances: IsolatedInstances) -> Iterator[None]:
    """Name all three databases to the smoke for the length of the run, then restore.

    The harness reads ``ATLAS_ERP_DATABASE_URL``, ``ATLAS_ECOM_DATABASE_URL`` and
    ``ATLAS_ERP_CONNECT_DATABASE_URL`` from this process and forwards its
    environment to the ERP server and to the Ecom clients, so setting them here is
    what puts each database on its own instance.  The three variables are
    separate on purpose: one URL cannot be two databases.  The values live in
    this process's environment only.
    """

    names = (
        ERP_DATABASE_ENV,
        cross_process_smoke.ECOM_DATABASE_ENV,
        CONNECT_DATABASE_ENV,
    )
    previous: Mapping[str, str | None] = {
        name: os.environ.get(name) for name in names
    }
    os.environ[ERP_DATABASE_ENV] = instances.erp.dsn
    os.environ[cross_process_smoke.ECOM_DATABASE_ENV] = instances.ecom.dsn
    os.environ[CONNECT_DATABASE_ENV] = instances.connect.dsn
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _require(condition: object, message: str) -> None:
    """Assert, in a form that names what was expected and what was found."""

    if not condition:
        raise AssertionError(message)


def _rows(
    instance: Instance, statement: str, *parameters: object
) -> list[tuple[Any, ...]]:
    # The statements are this file's own literals; the only interpolated name is
    # a table this script chose, never a caller-supplied value.
    query: Any = statement
    with psycopg.connect(instance.dsn, connect_timeout=5) as connection:
        return connection.execute(query, parameters).fetchall()


def _scalar(instance: Instance, statement: str, *parameters: object) -> Any:
    rows = _rows(instance, statement, *parameters)
    return rows[0][0] if rows else None


def _public_tables(instance: Instance) -> tuple[str, ...]:
    """The tables this connection can actually see, read from the catalog."""

    return tuple(
        str(row[0])
        for row in _rows(
            instance,
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public' ORDER BY table_name",
        )
    )


def _isolated_databases(instance: Instance) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in _rows(
            instance,
            "SELECT datname FROM pg_database WHERE datname = ANY(%s) ORDER BY datname",
            list(ISOLATED_DATABASES),
        )
    )


def _attempt(instance: Instance, statement: str) -> str | None:
    """Run one statement and return the server's refusal, or None if it worked."""

    try:
        query: Any = statement
        with psycopg.connect(instance.dsn, connect_timeout=5) as connection:
            connection.execute(query).fetchall()
    except psycopg.Error as error:
        detail = " ".join(str(error).strip().splitlines()[:1])
        return _scrub(detail, instance.password)
    return None


def check_instances_are_separate(instances: IsolatedInstances) -> dict[str, object]:
    """Three servers, three ports, and one database in each."""

    held = {instance.database: _isolated_databases(instance) for instance in instances.all}
    ports = {instance.port for instance in instances.all}
    _require(
        len(ports) == 3,
        f"two instances were given the same port: {sorted(ports)}",
    )
    for instance in instances.all:
        _require(
            held[instance.database] == (instance.database,),
            f"the {instance.database} instance holds {held[instance.database]},"
            f" expected only {instance.database!r}",
        )
    return {
        "check": "instances_are_separate",
        "erp_instance": instances.erp.name,
        "ecom_instance": instances.ecom.name,
        "connect_instance": instances.connect.name,
        "erp_port": instances.erp.port,
        "ecom_port": instances.ecom.port,
        "connect_port": instances.connect.port,
        "erp_product_databases": list(held[ERP_DATABASE]),
        "ecom_product_databases": list(held[ECOM_DATABASE]),
        "connect_product_databases": list(held[CONNECT_DATABASE]),
    }


def check_peer_credentials_are_refused(
    instances: IsolatedInstances,
) -> dict[str, object]:
    """No account authenticates on any instance but its own.

    That is six attempts, not two: every product is refused by the other product
    *and* by the protocol store, and the protocol store's own account is refused
    by both products.  A product that held a login on the protocol instance would
    be one query away from every peer's decisions, and a protocol store that
    shared a server with a product would make the first two refusals moot.
    """

    refusals: dict[str, str] = {}
    for owner in instances.all:
        for intruder in instances.all:
            if owner is intruder:
                continue
            attempt = intruder.credentials_on(owner.port, owner.database)
            refusal = _attempt_credentials(attempt, intruder.password)
            _require(
                refusal is not None,
                f"{intruder.user} authenticated on the {owner.database} instance,"
                f" so it holds a login on another store's server",
            )
            refusals[f"{intruder.user}@{owner.database}"] = cast(str, refusal)
    return {
        "check": "peer_credentials_are_refused",
        "refusals": refusals,
    }


def _attempt_credentials(dsn: str, password: str) -> str | None:
    """Return the server's refusal of a login, or None if it authenticated."""

    try:
        with psycopg.connect(dsn, connect_timeout=5):
            return None
    except psycopg.Error as error:
        return _scrub(" ".join(str(error).strip().splitlines()[:1]), password)


def check_cross_database_reference_is_refused(
    instances: IsolatedInstances,
) -> dict[str, object]:
    """A direct cross-database reference is refused by the server itself.

    The reference is three-part, ``database.schema.table``, because that is the
    form PostgreSQL treats as a cross-database reference.  A two-part
    ``database.table`` is only ``schema.table``, and it fails as a missing
    schema, which would prove nothing about the boundary.

    The same statement text is run on both connections, and the other instance's
    own connection must accept it.  Without that control a refusal would prove only
    that the statement was written wrong; with it, the only difference between the
    two answers is which server was asked.  The refusal is also required to be the
    server's own cross-database refusal and not some other error, so a typo cannot
    satisfy this check.  No extension is installed, so nothing is left behind that
    could make the attempt possible later.

    All six ordered pairs are attempted, so the protocol store is a boundary in
    both directions rather than only from a product's side.
    """

    tables = {instance.database: _public_tables(instance) for instance in instances.all}
    for database, found in tables.items():
        _require(found, f"the {database} instance holds no tables to reference")
    refusals: dict[str, str] = {}
    for owner in instances.all:
        for peer in instances.all:
            if owner is peer:
                continue
            direction = f"{owner.database}->{peer.database}"
            reference = f"{peer.database}.public.{tables[peer.database][0]}"
            statement = f"SELECT count(*) FROM {reference}"
            _require(
                _attempt(peer, statement) is None,
                f"the same statement was refused on the {peer.database} instance,"
                f" so the refusal below is not about the database boundary",
            )
            refusal = _attempt(owner, statement)
            _require(
                refusal is not None,
                f"the {owner.database} instance answered a query against {reference}",
            )
            _require(
                CROSS_DATABASE_REFUSAL in cast(str, refusal),
                f"the {owner.database} instance refused {reference} for an unrelated"
                f" reason: {refusal}",
            )
            refusals[direction] = cast(str, refusal)
    return {
        "check": "cross_database_reference_is_refused",
        "refusals": refusals,
    }


def check_each_product_sees_only_its_own_tables(
    instances: IsolatedInstances,
) -> dict[str, object]:
    """Each product connection sees its own schema in full and nobody else's.

    The protocol table is a third thing here, and it is the one that matters
    most: a product instance that could see ``connect_proposals`` could read every
    peer's decisions, so the ERP and Ecom connections are checked against it as
    well as against each other.
    """

    erp_tables = set(_public_tables(instances.erp))
    ecom_tables = set(_public_tables(instances.ecom))
    missing = [table for table in ERP_TABLES if table not in erp_tables]
    _require(
        not missing,
        f"the ERP connection cannot see its own tables: {missing}",
    )
    leaked = sorted(ecom_tables & erp_tables)
    _require(
        not leaked,
        f"the ERP connection can see the Ecom tables {leaked}",
    )
    _require(
        not (erp_tables & ecom_tables),
        f"the Ecom connection can see the ERP tables {sorted(erp_tables & ecom_tables)}",
    )
    for owner, product_tables, name in (
        (instances.erp, erp_tables, "ERP"),
        (instances.ecom, ecom_tables, "Ecom"),
    ):
        held = sorted(product_tables & set(CONNECT_TABLES))
        _require(
            not held,
            f"the {name} instance holds the protocol's tables {held}",
        )
    return {
        "check": "each_product_sees_only_its_own_tables",
        "erp_tables": sorted(erp_tables),
        "ecom_tables": sorted(ecom_tables),
        "erp_declared_tables": list(ERP_TABLES),
        "erp_holds_protocol_tables": sorted(erp_tables & set(CONNECT_TABLES)),
        "ecom_holds_protocol_tables": sorted(ecom_tables & set(CONNECT_TABLES)),
    }


def check_the_protocol_store_holds_no_product_state(
    instances: IsolatedInstances,
) -> dict[str, object]:
    """The protocol instance holds the decision and nothing a product owns."""

    connect_tables = set(_public_tables(instances.connect))
    missing = [table for table in CONNECT_TABLES if table not in connect_tables]
    _require(
        not missing,
        f"the protocol instance cannot see its own table: {missing}",
    )
    product_tables = sorted(connect_tables & set(ERP_TABLES))
    _require(
        not product_tables,
        f"the protocol store holds product tables {product_tables}",
    )
    # The Ecom table names are read from the Ecom instance rather than listed, so
    # this cannot claim a property it never looked at.
    ecom_tables = set(_public_tables(instances.ecom))
    leaked = sorted(connect_tables & ecom_tables)
    _require(
        not leaked,
        f"the protocol store holds Ecom's tables {leaked}",
    )
    return {
        "check": "the_protocol_store_holds_no_product_state",
        "connect_tables": sorted(connect_tables),
        "connect_declared_tables": list(CONNECT_TABLES),
        "connect_holds_erp_tables": product_tables,
        "connect_holds_ecom_tables": leaked,
    }


def check_the_sale_travelled_through_the_protocol(
    instances: IsolatedInstances, results: Mapping[str, Mapping[str, object]]
) -> dict[str, object]:
    """The sale is in the ERP instance, the order in the Ecom one, and nothing else.

    Each side is checked in the database that owns the record, and then checked
    again in the peer's database for the absence of anything it did not own.  The
    decision that authorised the sale is checked in the third database: it is a
    record of its own, and the point of this check now is that the sale and the
    proposal that recognised it live in different databases and neither instance
    can see the other's table.
    """

    checkout = results.get("connected-checkout-smoke", {})
    order_smoke = results.get("connected-order-smoke", {})
    sale_id = str(checkout.get("sale_id", ""))
    order_id = str(checkout.get("order_id", ""))
    item_id = str(checkout.get("sold_item_id", ""))
    _require(sale_id, "the connected checkout reported no sale id")
    _require(order_id, "the connected checkout reported no order id")

    erp_sales = _scalar(
        instances.erp, "SELECT count(*) FROM sales WHERE sale_id = %s", sale_id
    )
    _require(
        erp_sales == 1,
        f"the ERP instance holds {erp_sales} sales named {sale_id!r}, expected 1",
    )
    erp_receipt = _scalar(
        instances.erp,
        "SELECT count(*) FROM connected_sale_commands"
        " WHERE sale_id = %s AND status = 'completed'",
        sale_id,
    )
    _require(
        erp_receipt == 1,
        f"the ERP instance holds {erp_receipt} completed receipts for {sale_id!r}",
    )
    erp_line = _scalar(
        instances.erp,
        "SELECT quantity FROM sale_lines WHERE sale_id = %s AND item_id = %s",
        sale_id,
        item_id,
    )
    _require(
        erp_line == 1,
        f"the ERP sale line for {item_id!r} is {erp_line!r}, expected the sold quantity 1",
    )
    erp_movement = _scalar(
        instances.erp,
        "SELECT quantity_delta FROM stock_movements WHERE reference_id = %s",
        sale_id,
    )
    _require(
        erp_movement == -1,
        f"the ERP stock movement for {sale_id!r} is {erp_movement!r}, expected -1",
    )
    erp_journal = _scalar(
        instances.erp,
        "SELECT sum(debit_cents) = sum(credit_cents) FROM journal_lines"
        " WHERE journal_id = (SELECT journal_id FROM sales WHERE sale_id = %s)",
        sale_id,
    )
    _require(erp_journal is True, "the ERP journal for the connected sale is not balanced")

    ecom_orders = _scalar(
        instances.ecom,
        "SELECT count(*) FROM connected_orders"
        " WHERE order_id = %s AND status = 'completed'",
        order_id,
    )
    _require(
        ecom_orders == 1,
        f"the Ecom instance holds {ecom_orders} completed orders named {order_id!r}",
    )
    # The whole of what Ecom knows about the sale is an id the protocol carried.
    ecom_sale_id = _scalar(
        instances.ecom,
        "SELECT response ->> 'remote_sale_id' FROM connected_orders WHERE order_id = %s",
        order_id,
    )
    _require(
        ecom_sale_id == sale_id,
        f"the Ecom order names {ecom_sale_id!r} as its remote sale, expected {sale_id!r}",
    )

    # The decision is in the protocol's own database, in a row that names the
    # peer that proposed it, the sale it carried, and the reason it was accepted.
    # One row per proposal, so a second attempt would show up as two.
    proposals = _rows(
        instances.connect,
        "SELECT peer_id, capability, state, reason, payload ->> 'sale_id',"
        " created_at IS NOT NULL, decided_at IS NOT NULL"
        " FROM connect_proposals ORDER BY created_at",
    )
    _require(
        proposals,
        "the protocol store holds no proposal for the connected checkout",
    )
    matched = [row for row in proposals if row[4] == sale_id]
    _require(
        len(matched) == 1,
        f"the protocol store holds {len(matched)} proposals for {sale_id!r},"
        f" expected 1; the whole table is {proposals}",
    )
    decision = matched[0]
    _require(
        (decision[0], decision[1], decision[2]) == (CONNECTED_PEER, "sales.manual_sales", "accepted"),
        f"the proposal for {sale_id!r} is {decision}, expected an accepted"
        f" {CONNECTED_PEER} proposal for sales.manual_sales",
    )
    _require(
        decision[5] is True and decision[6] is True,
        f"the proposal for {sale_id!r} has no created_at/decided_at: {decision}",
    )
    _require(
        decision[3] == f"posted {sale_id}",
        f"the proposal for {sale_id!r} records the reason {decision[3]!r}",
    )

    # No instance can even name another's table, so none can hold its rows:
    # absence of the table is the strongest form of "not our records".
    erp_tables = set(_public_tables(instances.erp))
    ecom_tables = set(_public_tables(instances.ecom))
    connect_tables = set(_public_tables(instances.connect))
    erp_holds = sorted(erp_tables & ecom_tables) + sorted(erp_tables & connect_tables)
    ecom_holds = sorted(ecom_tables & erp_tables) + sorted(ecom_tables & connect_tables)
    connect_holds = sorted(connect_tables & erp_tables) + sorted(
        connect_tables & ecom_tables
    )
    _require(
        not erp_holds,
        f"the ERP instance holds another store's tables {erp_holds}",
    )
    _require(
        not ecom_holds,
        f"the Ecom instance holds another store's tables {ecom_holds}",
    )
    _require(
        not connect_holds,
        f"the protocol store holds a product's tables {connect_holds}",
    )
    return {
        "check": "the_sale_travelled_through_the_protocol",
        "erp_sale_id": sale_id,
        "ecom_order_id": order_id,
        "erp_instance_sales_row": erp_sales,
        "erp_instance_receipt_row": erp_receipt,
        "erp_instance_stock_movement": erp_movement,
        "erp_instance_journal_balanced": erp_journal,
        "ecom_instance_order_row": ecom_orders,
        "ecom_instance_names_erp_sale_as": ecom_sale_id,
        "connect_instance_proposal_rows": len(proposals),
        "connect_instance_proposal_peer": decision[0],
        "connect_instance_proposal_state": decision[2],
        "connect_instance_proposal_reason": decision[3],
        "connect_instance_proposal_sale_id": decision[4],
        "connect_instance_proposal_timestamped": [decision[5], decision[6]],
        "erp_holds_another_store_tables": erp_holds,
        "ecom_holds_another_store_tables": ecom_holds,
        "connect_holds_product_tables": connect_holds,
    }


def _provision_peer_roles(
    connect: Instance, peers: tuple[PeerCredential, ...]
) -> None:
    """Create one login per peer and grant it the table privileges it needs.

    The grant is the point: a peer is given ``SELECT`` and ``UPDATE`` on the
    proposal table, so the *only* thing standing between it and another peer's row
    is the row policy.  A refusal that came from a missing privilege would prove
    nothing about isolation.

    ``CREATE ROLE`` is a utility statement, so PostgreSQL will not take a
    parameter for a password; the value goes through ``sql.Literal`` instead,
    which quotes it rather than pasting it, and a failure is reported with every
    password scrubbed because a server error can echo the statement it refused.
    """

    try:
        with psycopg.connect(connect.dsn, connect_timeout=5) as connection:
            for peer in peers:
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(peer.peer_id), sql.Literal(peer.password)
                    )
                )
            for table in CONNECT_TABLES:
                for peer in peers:
                    grant: Any = f"GRANT SELECT, UPDATE ON {table} TO {peer.role}"
                    connection.execute(grant)
    except psycopg.Error as error:
        # ``from None``: the chained cause would print the statement, and the
        # statement carries the password.
        raise RuntimeError(
            _scrub(str(error), *(peer.password for peer in peers))
        ) from None


def _peer_credentials() -> tuple[PeerCredential, ...]:
    return tuple(
        PeerCredential(peer_id=peer_id, password=secrets.token_urlsafe(24))
        for peer_id in (CONNECTED_PEER, SECOND_PEER)
    )


def _peer_rows(peer: PeerCredential, connect: Instance, peer_id: str) -> int:
    """How many rows this peer's own login can see for ``peer_id``."""

    with psycopg.connect(peer.dsn(connect), connect_timeout=5) as connection:
        rows = connection.execute(
            "SELECT count(*) FROM connect_proposals WHERE peer_id = %s", (peer_id,)
        ).fetchall()
    return int(rows[0][0])


def _peer_update_rows(
    peer: PeerCredential, connect: Instance, proposal_id: str, reason: str
) -> int:
    """Decide one row as this peer's own login and report how many rows changed.

    The statement sets the state, the reason, and the decision time together,
    because the table's check constraint refuses a record that claims a decision
    without saying why -- and because the own-row control and the cross-peer
    attempt below have to be the *same* statement, or the zero would be about the
    statement rather than about the row policy.

    The transaction is rolled back, so the check cannot leave a peer's record
    rewritten even when the update is allowed.
    """

    with psycopg.connect(peer.dsn(connect), connect_timeout=5) as connection:
        changed = connection.execute(
            "UPDATE connect_proposals"
            " SET state = 'rejected', reason = %s, decided_at = now()"
            " WHERE proposal_id = %s",
            (reason, proposal_id),
        ).rowcount
        connection.rollback()
    return int(changed)


def check_peer_rows_are_confined_to_their_own_peer(
    instances: IsolatedInstances, sale_id: str
) -> dict[str, object]:
    """One peer's login cannot read or update another peer's proposal.

    The isolation is enforced by PostgreSQL, not by a ``WHERE peer = ?`` in
    application code, and this is the evidence for that: a real login as one peer
    is pointed at another peer's row.  Row-level security filters rather than
    raises, so "it failed" here means *zero rows read and zero rows changed*, and
    each direction also carries two positive controls -- the same login sees its
    own row, and the same ``UPDATE`` changes its own row -- so a zero can only be
    the policy and not a statement written wrong.

    The second peer's row is written by the owner through the store's own API,
    which is what the adapter does when a peer proposes something.  Nothing here
    connects ``atlas-hq`` to anything.
    """

    peers = _peer_credentials()
    _provision_peer_roles(instances.connect, peers)
    owner = _open_proposal_store(instances.connect)
    try:
        second = owner.create(
            f"smoke-{SECOND_PEER}-proposal",
            SECOND_PEER,
            "sales.manual_sales",
            {"customer_id": "customer-1", "sale_id": f"{sale_id}-hq"},
        )
        first = _scalar(
            instances.connect,
            "SELECT proposal_id FROM connect_proposals"
            " WHERE peer_id = %s AND payload ->> 'sale_id' = %s",
            CONNECTED_PEER,
            sale_id,
        )
        _require(first, f"the protocol store holds no {CONNECTED_PEER} proposal for the sale")
        first_id = str(first)
        before = {
            proposal_id: reason
            for proposal_id, reason in _rows(
                instances.connect,
                "SELECT proposal_id, reason FROM connect_proposals"
                " WHERE proposal_id = ANY(%s)",
                [first_id, second.proposal_id],
            )
        }
    finally:
        owner.close()

    evidence: dict[str, object] = {}
    for peer, other, other_id in (
        (peers[0], SECOND_PEER, second.proposal_id),
        (peers[1], CONNECTED_PEER, first_id),
    ):
        _require(
            _peer_rows(peer, instances.connect, peer.peer_id) >= 1,
            f"{peer.peer_id} cannot see its own proposals, so the row policy is"
            f" not the thing under test here",
        )
        own_changed = _peer_update_rows(
            peer, instances.connect, _own_proposal_id(peer, instances.connect), "self"
        )
        _require(
            own_changed == 1,
            f"{peer.peer_id} could not update its own proposal ({own_changed} rows),"
            f" so the cross-peer zero below would prove nothing",
        )
        cross_read = _peer_rows(peer, instances.connect, other)
        _require(
            cross_read == 0,
            f"{peer.peer_id} read {cross_read} of {other}'s proposals",
        )
        cross_changed = _peer_update_rows(
            peer, instances.connect, other_id, f"rewritten by {peer.peer_id}"
        )
        _require(
            cross_changed == 0,
            f"{peer.peer_id} changed {cross_changed} of {other}'s proposals",
        )
        evidence[f"{peer.peer_id}_sees_own"] = True
        evidence[f"{peer.peer_id}_updates_own_rows"] = own_changed
        evidence[f"{peer.peer_id}_sees_{other}"] = cross_read
        evidence[f"{peer.peer_id}_updates_{other}_rows"] = cross_changed

    # The owner's own reasons are untouched, read on a connection that is not
    # filtered: a peer's refused write has to have changed nothing at all.
    after = {
        proposal_id: reason
        for proposal_id, reason in _rows(
            instances.connect,
            "SELECT proposal_id, reason FROM connect_proposals"
            " WHERE proposal_id = ANY(%s)",
            [first_id, second.proposal_id],
        )
    }
    _require(
        after == before,
        f"a refused cross-peer update changed the owner's rows: {before} -> {after}",
    )
    return {
        "check": "peer_rows_are_confined_to_their_own_peer",
        "peers": [peer.shape for peer in peers],
        "own_row_update_is_allowed": True,
        "owner_reasons_unchanged": after == before,
        **evidence,
    }


def _own_proposal_id(peer: PeerCredential, connect: Instance) -> str:
    """The one proposal id this peer's own login can see, for its own-row control."""

    with psycopg.connect(peer.dsn(connect), connect_timeout=5) as connection:
        rows = connection.execute(
            "SELECT proposal_id FROM connect_proposals WHERE peer_id = %s"
            " ORDER BY created_at LIMIT 1",
            (peer.peer_id,),
        ).fetchall()
    if not rows:
        raise AssertionError(f"{peer.peer_id} has no proposal of its own to update")
    return str(rows[0][0])


def _open_proposal_store(connect: Instance) -> PostgresProposalStore:
    """The store as the adapter opens it, on the owner account of this instance."""

    return PostgresProposalStore(connect.dsn)


def _run_proposal_store_tests() -> dict[str, object]:
    """Run the database-backed proposal-store tests against these instances.

    The store tests are gated on the same environment variables the transport
    reads, so pointing them at the containers this script just provisioned is
    what turns "skipped: no database" into the real durability, replay, and
    refusal cases.  They run in a child process so a failure there cannot leave
    this process holding a connection to an instance it is about to remove.
    """

    result = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "tests.test_proposal_store", "-v"],
        cwd=ERP_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=STORE_TEST_TIMEOUT_SECONDS,
        check=False,
        env=os.environ.copy(),
    )
    tail = [line for line in (result.stderr or result.stdout).splitlines() if line.strip()]
    _require(
        result.returncode == 0,
        "the proposal store tests failed against the isolated instances:\n"
        + "\n".join(tail[-90:]),
    )
    return {
        "check": "proposal_store_tests",
        # The last three non-empty lines are the count, the blank, and the verdict.
        "summary": " | ".join(line.strip() for line in tail[-3:]),
        "returncode": result.returncode,
    }


def _report(evidence: Mapping[str, object]) -> None:
    print(json.dumps({"ok": True, **evidence}, sort_keys=True), flush=True)


def run() -> int:
    """Provision, check, run the smoke, and tear down.  Returns a shell status."""

    with isolated_instances() as instances:
        _report(
            {
                "erp_dsn": instances.erp.shape,
                "ecom_dsn": instances.ecom.shape,
                "connect_dsn": instances.connect.shape,
            }
        )
        _report(check_instances_are_separate(instances))
        _report(check_peer_credentials_are_refused(instances))
        with _database_environment(instances):
            results = cross_process_smoke.run_smoke()
            _report(check_cross_database_reference_is_refused(instances))
            _report(check_each_product_sees_only_its_own_tables(instances))
            _report(check_the_protocol_store_holds_no_product_state(instances))
            _report(check_the_sale_travelled_through_the_protocol(instances, results))
            sale_id = str(
                results.get("connected-checkout-smoke", {}).get("sale_id", "")
            )
            _report(check_peer_rows_are_confined_to_their_own_peer(instances, sale_id))
            _report(_run_proposal_store_tests())
    return 0


def main() -> int:
    try:
        return run()
    except Exception as exc:
        print(f"two-instance smoke failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

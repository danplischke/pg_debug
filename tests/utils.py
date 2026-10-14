import hashlib
import logging
import os
import time
from collections.abc import Generator
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import docker
import psycopg
import pytest
from docker import errors as docker_errors
from docker.models.containers import Container
from psycopg import sql

logger = logging.getLogger(__name__)


#: PgBouncer as the managed platforms run it in front of PostgreSQL: transaction
#: pooling, with no support for prepared statements. Round robin makes each of a
#: client's transactions go to the next backend, which is what concurrent load
#: does to a real pooler, made deterministic. Three backends per database and
#: role, fewer than pg-debug's pool of five, so they are shared.
PGBOUNCER_POOL_SIZE = 3
PGBOUNCER_INI = f"""\
[databases]
* = host=127.0.0.1 port=5432

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
unix_socket_dir =
auth_type = trust
auth_file = /tmp/pgbouncer/userlist.txt
admin_users = postgres
pool_mode = transaction
max_prepared_statements = 0
server_round_robin = 1
default_pool_size = {PGBOUNCER_POOL_SIZE}
max_client_conn = 200
ignore_startup_parameters = extra_float_digits
logfile = /tmp/pgbouncer/pgbouncer.log
pidfile = /tmp/pgbouncer/pgbouncer.pid
"""


def create_postgres_container(version: str) -> Generator[tuple[str, str], None, None]:
    """Create a PostgreSQL container of specified version and return its connection string."""
    with _postgres_container(version) as (_, connection_string):
        yield connection_string, version


def create_pooled_postgres_container(version: str) -> Generator[tuple[str, str], None, None]:
    """A PostgreSQL container with PgBouncer in front of it, as :data:`PGBOUNCER_INI` configures it.

    Yields:
        The connection string to the server, and the one to the pooler.
    """
    with _postgres_container(version) as (container, connection_string):
        yield connection_string, _start_pgbouncer(container, connection_string)


def _start_pgbouncer(container, connection_string: str) -> str:
    """Start PgBouncer inside the container and return its connection string."""
    script = (
        "mkdir -p /tmp/pgbouncer && "
        "printf '%s' \"$PGBOUNCER_INI\" > /tmp/pgbouncer/pgbouncer.ini && "
        'printf \'"postgres" ""\\n\' > /tmp/pgbouncer/userlist.txt && '
        "pgbouncer -d /tmp/pgbouncer/pgbouncer.ini"
    )
    # PgBouncer will not run as root.
    exit_code, output = container.exec_run(["sh", "-c", script], user="postgres", environment={"PGBOUNCER_INI": PGBOUNCER_INI})
    if exit_code != 0:
        pytest.fail(f"PgBouncer did not start (an image built before it was added needs rebuilding): {output.decode(errors='replace')}")
    port = container.ports["6432/tcp"][0]["HostPort"]
    pooled = connection_string.replace(f":{urlsplit(connection_string).port}/", f":{port}/")
    last_error = _wait_until_ready(pooled, container, "PgBouncer")
    if last_error is not None:
        pytest.fail(f"Timeout waiting for PgBouncer to start: {last_error}")
    return pooled


def _wait_until_ready(connection_string: str, container: Container, what: str) -> str | None:
    """Wait for a connection to succeed, and return None, or the last error after a minute."""
    deadline = time.time() + 60
    last_error = None

    while time.time() < deadline:
        # Ready means a connection from here, through the mapped port.
        # pg_isready inside the container also answers for the temporary
        # server the image runs its init scripts on, which listens on a
        # Unix socket only, and a test that connected on its word met that
        # server shutting down: "server closed the connection unexpectedly".
        try:
            with psycopg.connect(connection_string, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            logger.info("%s in container %s is ready", what, container.name)
            return None
        except psycopg.Error as e:
            last_error = str(e)
            logger.warning("%s not ready yet: %s", what, last_error)

        # Get container logs for debugging
        if time.time() - deadline + 60 > 50:  # Log when we're close to timeout
            logs = container.logs().decode("utf-8")
            logger.warning("Still waiting for %s. Container logs:\n%s", what, logs[-2000:])

        time.sleep(2)

    logs = container.logs().decode("utf-8")
    logger.error("Timeout waiting for %s. Container logs:\n%s", what, logs[-2000:])
    return last_error


@contextmanager
def _postgres_container(version: str) -> Iterator[tuple[Container, str]]:
    """Run a PostgreSQL container of the specified version until the block ends."""
    try:
        client = docker.from_env()
        client.ping()
        os_type = client.info().get("OSType")
    except (docker_errors.DockerException, ConnectionError):
        pytest.skip("Docker is not available")
    # The PostgreSQL images are Linux images. A Docker running Windows
    # containers, as on GitHub's Windows runners, would fail to build them.
    if os_type != "linux":
        pytest.skip(f"Docker runs {os_type} containers here, and the PostgreSQL images are Linux images")

    # Extract PostgreSQL version number
    pg_version = version.split(":")[1] if ":" in version else version

    current_dir = Path(__file__).parent.absolute()

    # Named for the Dockerfile's content as well as the version, so that an
    # image built from an older Dockerfile is not reused after it changes.
    dockerfile_digest = hashlib.sha256((current_dir / "Dockerfile.postgres-hypopg").read_bytes()).hexdigest()[:12]
    custom_image_name = f"postgres-hypopg:{pg_version}-{dockerfile_digest}"

    container_name = f"postgres-crystal-test-{version.replace(':', '_')}-{os.urandom(4).hex()}"

    logger.info("Setting up PostgreSQL %s with HypoPG", pg_version)

    # Build custom Docker image with HypoPG if it doesn't exist
    try:
        # Check if custom image already exists
        client.images.get(custom_image_name)
        logger.info("Using existing Docker image: %s", custom_image_name)
    except docker_errors.ImageNotFound:
        # Build the custom image
        logger.info("Building custom Docker image: %s", custom_image_name)
        try:
            dockerfile_path = current_dir / "Dockerfile.postgres-hypopg"
            if not dockerfile_path.exists():
                logger.error("Dockerfile not found at %s", dockerfile_path)
                pytest.skip(f"Required Dockerfile not found: {dockerfile_path}")

            # Build the image
            client.images.build(
                path=str(current_dir),
                dockerfile="Dockerfile.postgres-hypopg",
                buildargs={"PG_VERSION": pg_version, "PG_MAJOR": pg_version},
                tag=custom_image_name,
                rm=True,
            )
            logger.info("Successfully built image %s", custom_image_name)
        except Exception as e:
            logger.error("Failed to build Docker image: %s", e)
            pytest.skip(f"Failed to build Docker image: {e}")

    postgres_password = "test_password"
    postgres_db = "test_db"

    # Create container with more verbose logging
    container = client.containers.run(
        custom_image_name,
        name=container_name,
        environment={
            "POSTGRES_PASSWORD": postgres_password,
            "POSTGRES_DB": postgres_db,
            "POSTGRES_HOST_AUTH_METHOD": "trust",  # Make authentication easier in tests
        },
        # Let Docker assign random ports: PostgreSQL's, and PgBouncer's for the
        # tests that start it.
        ports={"5432/tcp": ("127.0.0.1", 0), "6432/tcp": ("127.0.0.1", 0)},
        command=[
            "-c",
            "shared_preload_libraries=pg_stat_statements",
            "-c",
            "pg_stat_statements.track=all",
            "-c",
            "log_min_messages=info",  # More verbose logging
            "-c",
            "log_statement=all",  # Log all SQL statements
        ],
        detach=True,
    )

    logger.info("Container %s started, waiting for PostgreSQL to be ready", container_name)

    try:
        # Wait for container to start and get logs
        time.sleep(2)  # Give container a moment to start
        container.reload()

        # Check if container is running
        if container.status != "running":
            logs = container.logs().decode("utf-8")
            logger.error("Container %s failed to start. Logs:\n%s", container_name, logs)
            pytest.skip(f"PostgreSQL container failed to start: {logs[:500]}...")

        # Get assigned port
        port = container.ports["5432/tcp"][0]["HostPort"]
        connection_string = f"postgresql://postgres:{postgres_password}@localhost:{port}/{postgres_db}"

        last_error = _wait_until_ready(connection_string, container, "PostgreSQL")
        if last_error is not None:
            pytest.skip(f"Timeout waiting for PostgreSQL to start: {last_error}")

        logger.info("PostgreSQL connection string: %s", connection_string)

        yield container, connection_string

    except Exception as e:
        logger.error("Error setting up PostgreSQL container: %s", e)
        # Get container logs for debugging
        try:
            logs = container.logs().decode("utf-8")
            logger.error("Container logs:\n%s", logs)
        except Exception:
            pass
        raise

    finally:
        logger.info("Stopping and removing container %s", container_name)
        try:
            container.stop(timeout=1)
            container.remove(v=True)
        except Exception as e:
            logger.warning("Error cleaning up container %s: %s", container_name, e)


#: The bytes of "café" in LATIN1. 0xe9 on its own is no character in UTF-8.
LATIN1_CAFE = "\\x636166e9"


@contextmanager
def sql_ascii_database(url: str) -> Iterator[str]:
    """A SQL_ASCII database beside the one ``url`` names, until the block ends.

    SQL_ASCII declares no encoding: the server stores and sends the bytes it
    was given. ``places`` holds "Zürich" in UTF-8, and ``legacy`` holds "café"
    in LATIN1, which is not UTF-8.

    Yields:
        The URL of the database, named after the one ``url`` names.
    """
    parts = urlsplit(url)
    name = f"{parts.path.lstrip('/')}_sql_ascii"
    database = sql.Identifier(name)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(database))
        conn.execute(sql.SQL("CREATE DATABASE {} ENCODING 'SQL_ASCII' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0").format(database))
    database_url = urlunsplit(parts._replace(path=f"/{name}"))
    try:
        with psycopg.connect(database_url, autocommit=True, client_encoding="UTF8") as conn:
            conn.execute("CREATE TABLE places (name text)")
            conn.execute("INSERT INTO places VALUES ('Zürich')")
            conn.execute("CREATE TABLE legacy (label text)")
            conn.execute(sql.SQL("INSERT INTO legacy VALUES (convert_from({}::bytea, 'SQL_ASCII'))").format(sql.Literal(LATIN1_CAFE)))
        yield database_url
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            # Before PostgreSQL 13 there is no DROP DATABASE ... WITH (FORCE).
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", [name])
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(database))

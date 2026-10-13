import logging
import os
import time
from collections.abc import Generator
from pathlib import Path

import docker
import pytest
from docker import errors as docker_errors

logger = logging.getLogger(__name__)


def create_postgres_container(version: str) -> Generator[tuple[str, str], None, None]:
    """Create a PostgreSQL container of specified version and return its connection string."""
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

    # Define custom image name with HypoPG
    custom_image_name = f"postgres-hypopg:{pg_version}"

    container_name = f"postgres-crystal-test-{version.replace(':', '_')}-{os.urandom(4).hex()}"
    current_dir = Path(__file__).parent.absolute()

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
        ports={"5432/tcp": ("127.0.0.1", 0)},  # Let Docker assign a random port
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

        # Wait for PostgreSQL to be ready
        deadline = time.time() + 60  # Increased timeout to 60 seconds
        is_ready = False
        last_error = None

        while time.time() < deadline and not is_ready:
            try:
                exit_code, output = container.exec_run("pg_isready")
                if exit_code == 0:
                    logger.info("PostgreSQL in container %s is ready", container_name)
                    is_ready = True
                    break
                else:
                    last_error = output.decode("utf-8")
                    logger.warning("PostgreSQL not ready yet: %s", last_error)
            except Exception as e:
                last_error = str(e)
                logger.warning("Error checking if PostgreSQL is ready: %s", e)

            # Get container logs for debugging
            if time.time() - deadline + 60 > 50:  # Log when we're close to timeout
                logs = container.logs().decode("utf-8")
                logger.warning("Still waiting for PostgreSQL. Container logs:\n%s", logs[-2000:])

            time.sleep(2)

        if not is_ready:
            logs = container.logs().decode("utf-8")
            logger.error("Timeout waiting for PostgreSQL. Container logs:\n%s", logs[-2000:])
            pytest.skip(f"Timeout waiting for PostgreSQL to start: {last_error}")

        connection_string = f"postgresql://postgres:{postgres_password}@localhost:{port}/{postgres_db}"
        logger.info("PostgreSQL connection string: %s", connection_string)

        yield connection_string, version

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

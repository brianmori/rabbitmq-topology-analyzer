"""Spawn-child entrypoint for one SDK operation and atomic result write."""

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

    from rabbitmq_management_sdk.topology import ClusterTopology

    from jobs.models import ResultEnvelopeContext, WorkerFunction
    from jobs.results import ResultWriteSpec


def run_operation_child(
    worker: WorkerFunction,
    topology: ClusterTopology,
    context: ResultEnvelopeContext,
    write_spec: ResultWriteSpec,
    connection: Connection,
) -> None:
    """Execute a domain bridge, reserve bytes with the parent, and publish."""
    try:
        payload = worker(topology, context)
        if not isinstance(payload, bytes) or not payload:
            raise TypeError("operation bridge returned an invalid result representation")
        connection.send(("reserve", len(payload)))
        response = connection.recv()
        if response != ("reservation", True):
            connection.send(
                (
                    "failure",
                    "result_storage_full",
                    "The completed result could not be retained within storage capacity.",
                ),
            )
            return
        with write_spec.temporary_path.open("xb") as result_file:
            result_file.write(payload)
            result_file.flush()
            os.fsync(result_file.fileno())
        write_spec.temporary_path.replace(write_spec.committed_path)
        connection.send(("published", len(payload)))
    except MemoryError:
        connection.send(
            ("failure", "worker_out_of_memory", "The analysis worker ran out of memory."),
        )
    except BrokenPipeError, EOFError, OSError:
        # The parent maps a broken control channel or failed publication after
        # process exit. There may be no receiver left for another safe message.
        return
    except Exception:
        connection.send(("failure", "worker_failed", "The analysis worker failed."))
    finally:
        connection.close()

"""Create the Nexus endpoint the optional Nexus path needs (idempotent).

The endpoint routes ``MoneyTransferService`` calls to the worker's task queue. It
only needs to exist when ``USE_NEXUS`` is set. Equivalent to the one-time CLI:

    temporal operator nexus endpoint create \\
        --name money-transfer-endpoint \\
        --target-namespace default \\
        --target-task-queue money-transfer

Run once against your dev server:

    uv run --env-file .env python scripts/setup_nexus.py
"""

from __future__ import annotations

import asyncio

from temporalio.api.nexus.v1 import EndpointSpec, EndpointTarget
from temporalio.api.operatorservice.v1 import (
    CreateNexusEndpointRequest,
    ListNexusEndpointsRequest,
)
from temporalio.client import Client

from skills.money_transfer.client import TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE
from skills.money_transfer.nexus_service import NEXUS_ENDPOINT
from skills.money_transfer.shared import TASK_QUEUE


async def main() -> None:
    client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
    operator = client.operator_service

    existing = await operator.list_nexus_endpoints(
        ListNexusEndpointsRequest(name=NEXUS_ENDPOINT, page_size=1)
    )
    if existing.endpoints:
        print(f"Nexus endpoint '{NEXUS_ENDPOINT}' already exists. Nothing to do.")
        return

    await operator.create_nexus_endpoint(
        CreateNexusEndpointRequest(
            spec=EndpointSpec(
                name=NEXUS_ENDPOINT,
                target=EndpointTarget(
                    worker=EndpointTarget.Worker(
                        namespace=TEMPORAL_NAMESPACE,
                        task_queue=TASK_QUEUE,
                    )
                ),
            )
        )
    )
    print(
        f"Created Nexus endpoint '{NEXUS_ENDPOINT}' -> "
        f"namespace '{TEMPORAL_NAMESPACE}', task queue '{TASK_QUEUE}'."
    )


if __name__ == "__main__":
    asyncio.run(main())

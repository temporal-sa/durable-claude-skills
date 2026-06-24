"""Temporal worker for the money-transfer skill.

Run this in its own process (or several, in production). It polls the
money-transfer task queue and executes the workflow and activities. The banking
activities are plain sync functions, so they run on a thread pool.
"""

from __future__ import annotations

import asyncio
import concurrent.futures

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from banking import service as bank
from skills.money_transfer.activities import (
    build_transfer_plan,
    deposit,
    refund,
    withdraw,
)
from skills.money_transfer.client import (
    TEMPORAL_ADDRESS,
    TEMPORAL_NAMESPACE,
    USE_NEXUS,
)
from skills.money_transfer.shared import TASK_QUEUE
from skills.money_transfer.workflow import MoneyTransferWorkflow


async def main() -> None:
    bank.seed()  # ensure the demo bank exists (idempotent)

    client = await Client.connect(
        TEMPORAL_ADDRESS,
        namespace=TEMPORAL_NAMESPACE,
        data_converter=pydantic_data_converter,
    )

    # The Nexus path adds a caller workflow and the operation handler. When the
    # flag is off, the worker is identical to the direct-path setup.
    workflows = [MoneyTransferWorkflow]
    nexus_service_handlers: list = []
    if USE_NEXUS:
        from skills.money_transfer.nexus_impl import (
            MoneyTransferServiceHandler,
            StartTransferWorkflow,
        )

        workflows.append(StartTransferWorkflow)
        nexus_service_handlers.append(MoneyTransferServiceHandler())

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as activity_executor:
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=workflows,
            activities=[build_transfer_plan, withdraw, deposit, refund],
            nexus_service_handlers=nexus_service_handlers,
            activity_executor=activity_executor,
            # Pydantic's core is a compiled (Rust) extension. The workflow sandbox
            # cannot re-import binary modules, so it warns when pydantic_core is
            # imported lazily during the first model validation inside a workflow.
            # Passing the pydantic modules through tells the sandbox to reuse the
            # host's already-imported copy, which is safe and silences the warning.
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "pydantic", "pydantic_core"
                )
            ),
        )
        mode = "direct + Nexus" if USE_NEXUS else "direct"
        print(
            f"Worker started on task queue '{TASK_QUEUE}' ({mode} path). "
            "Ctrl-C to stop."
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

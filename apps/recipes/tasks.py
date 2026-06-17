"""Background execution of recipe runs.

A recipe run drives the LangGraph agent headlessly and may block on a
materialization (loading fresh data before building a dashboard). That cannot
run inline in the HTTP request — it would hold the connection open for minutes
— so the API endpoint creates a PENDING ``RecipeRun`` and defers this task. The
frontend polls ``GET .../runs/<id>/`` until the run reaches a terminal status.
"""

from __future__ import annotations

import logging

from apps.recipes.models import RecipeRun, RecipeRunStatus
from apps.recipes.services.runner import RecipeRunner
from config.procrastinate import task

logger = logging.getLogger(__name__)


@task(pass_context=True, queue="recipes")
async def run_recipe(context, recipe_run_id: str) -> dict:
    """Execute a recipe run the API has already created (status PENDING).

    Runs ``RecipeRunner.execute_async`` (headless: ``interactive=False`` graph +
    blocking materialize), updating the existing ``RecipeRun`` in place so the
    client's poll reflects progress and the final result.

    Runs on the ``recipes`` queue so a long recipe run does not starve chat
    materializations — deploy a dedicated worker (``--queues recipes``) or raise
    worker concurrency if recipe and chat materialization load contend.
    """
    try:
        run = await RecipeRun.objects.select_related("recipe", "run_by").aget(id=recipe_run_id)
    except RecipeRun.DoesNotExist:
        logger.warning("run_recipe: RecipeRun %s not found", recipe_run_id)
        return {"status": "missing"}

    runner = RecipeRunner(
        recipe=run.recipe,
        variable_values=run.variable_values,
        user=run.run_by,
        run=run,
        job_id=context.job.id,
    )
    try:
        await runner.execute_async()
    except Exception:
        # execute_async records FAILED itself for agent errors, but guard
        # against anything it doesn't catch (e.g. re-validation) so the row is
        # never left stuck in PENDING/RUNNING with the client polling forever.
        logger.exception("run_recipe: unhandled error executing RecipeRun %s", recipe_run_id)
        run.status = RecipeRunStatus.FAILED
        await run.asave(update_fields=["status"])
        return {"status": RecipeRunStatus.FAILED}

    return {"status": run.status}

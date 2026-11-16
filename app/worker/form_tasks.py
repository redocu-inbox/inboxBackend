"""Form fill jobs.

ARQ job names are these function names. Arguments are strings. Mail jobs live
in mail_tasks.
"""

from app.services.forms.fill_job import execute_form_fill
from app.services.forms.match import execute_form_match


async def match_form_template(ctx: dict, job_id: str) -> None:
    """Choose a template, extract values, and enqueue a fill when the model returns some.

    A missing id is logged and ignored. The stub stops at needs_review and
    does not enqueue run_form_fill. This job does not call Graph or Gmail.
    """
    await execute_form_match(ctx, job_id)


async def run_form_fill(ctx: dict, job_id: str) -> None:
    """Write the job's saved values into the AcroForm and store the PDF.

    A missing id is logged and ignored. Running the job again overwrites the
    same storage key. Confidence is left as it was stored at create time.
    """
    del ctx
    execute_form_fill(job_id)

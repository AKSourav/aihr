from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from core.schemas import CallContext

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_system_prompt(call_context: CallContext) -> str:
    template = _env.get_template("system.md.j2")
    return template.render(
        company_name=call_context.company_name,
        job_title=call_context.job_title,
        candidate_first_name=call_context.candidate_first_name,
        question_plan=call_context.question_plan,
        max_minutes=call_context.max_minutes,
    )

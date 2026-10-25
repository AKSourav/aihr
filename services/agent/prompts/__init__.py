from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from core.schemas import CallContext, FaqItem, QuestionItem

_env = Environment(loader=FileSystemLoader(Path(__file__).parent))


def _render_questions(questions: list[QuestionItem]) -> str:
    lines = []
    for q in questions:
        line = f"- [{q.id}] {q.text}"
        if q.probe:
            line += f" Follow-up hint, ask only in a LATER turn if needed: {q.probe}"
        lines.append(line)
    return "\n".join(lines)


def _render_faq(faq: list[FaqItem]) -> str:
    return "\n".join(f"- Q: {f.q} A: {f.a}" for f in faq)


def render_system_prompt(call_context: CallContext) -> str:
    template = _env.get_template("system.md.j2")
    return template.render(
        company_name=call_context.company_name,
        job_title=call_context.job_title,
        candidate_first_name=call_context.candidate_first_name,
        max_minutes=call_context.max_minutes,
        questions_block=_render_questions(call_context.question_plan.questions),
        faq_block=_render_faq(call_context.question_plan.faq),
    )

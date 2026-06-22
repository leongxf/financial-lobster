from __future__ import annotations

from app.core.config import Settings
from app.skills.base import SkillRegistry
from app.skills.financial_summary_skill import FinancialSummarySkill
from app.skills.industry_research_skill import IndustryResearchSkill
from app.skills.qa_skill import QASkill
from app.skills.template_rewrite_skill import TemplateRewriteSkill


def build_registry(settings: Settings) -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(FinancialSummarySkill())
    registry.register(QASkill())
    if settings.enable_industry_research:
        registry.register(IndustryResearchSkill())
    if settings.enable_template_rewrite:
        registry.register(TemplateRewriteSkill())
    return registry

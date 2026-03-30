"""Pre-dispatch validation gate for autonomy actions."""

from genesis.autonomy.types import (
    CONTEXT_CEILING_MAP,
    ActionClass,
    ApprovalDecision,
    AutonomyCategory,
    ContextCeiling,
)

_CATEGORY_TO_CEILING = {
    AutonomyCategory.DIRECT_SESSION: ContextCeiling.DIRECT_SESSION,
    AutonomyCategory.BACKGROUND_COGNITIVE: ContextCeiling.BACKGROUND_COGNITIVE,
    AutonomyCategory.SUB_AGENT: ContextCeiling.SUB_AGENT,
    AutonomyCategory.OUTREACH: ContextCeiling.OUTREACH,
}


def check_dispatch_preconditions(
    category: str,
    required_level: int,
    action_class: ActionClass,
    earned_level: int,
) -> ApprovalDecision:
    """Validate whether an action may proceed, needs proposal, or is blocked."""
    try:
        ceiling_key = _CATEGORY_TO_CEILING[AutonomyCategory(category)]
    except (ValueError, KeyError):
        return ApprovalDecision.BLOCK
    ceiling = CONTEXT_CEILING_MAP.get(ceiling_key, 0)

    if required_level > ceiling:
        return ApprovalDecision.BLOCK
    if required_level > earned_level:
        return ApprovalDecision.BLOCK
    if action_class == ActionClass.IRREVERSIBLE:
        return ApprovalDecision.PROPOSE
    if action_class == ActionClass.COSTLY_REVERSIBLE and required_level >= 3:
        return ApprovalDecision.PROPOSE
    return ApprovalDecision.ACT

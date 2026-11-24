"""RenderQuantV2_Institutional Strategy alias."""
from user_data.strategies.RenderQuantV1 import RenderQuantV1


class RenderQuantV2_Institutional(RenderQuantV1):
    """Alias institucional dedicado para a versão 2 do RenderQuant (8% cap, 0.75% risco)."""

    _cached_strategy_mode: str = "RenderQuantV2_Institutional"

    def _get_active_strategy_mode(self) -> str:
        return "RenderQuantV2_Institutional"

"""跟单人风控配置与逐节点策略快照；不改变机器人手数。"""
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import strategy_templates


class FollowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    risk_mode: Literal["leader", "custom"] = "leader"
    loss_ratio: float | None = Field(default=None, gt=0, le=100)
    run_mode: Literal["loop", "single"] = "loop"

    @model_validator(mode="after")
    def validate_ratio(self):
        if self.risk_mode == "custom" and self.loss_ratio is None:
            raise ValueError("自定义信号盈亏比必须大于 0 且不超过 100%")
        if self.risk_mode == "leader":
            self.loss_ratio = None
        return self


def public_settings(raw: dict | None) -> dict:
    return FollowSettings.model_validate({key: value for key, value in (raw or {}).items()
                                         if key in FollowSettings.model_fields}).model_dump()


def apply_settings(snapshot: dict, settings: dict | None) -> dict:
    if not settings or settings.get("risk_mode") != "custom" or snapshot.get("template_id") != strategy_templates.TEMPLATE_1_ID:
        return snapshot
    result = deepcopy(snapshot)
    for rule in result.get("rules") or []:
        ratio = strategy_templates.default_signal_float_pl_ratio()
        ratio.update(enabled=True, ratio=-float(settings["loss_ratio"]))
        rule["float_pl_ratio"] = ratio
    return result

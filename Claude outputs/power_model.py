"""整机功耗估算模型。

真实传感器（nvidia-smi 的板卡功耗、RAPL 的 CPU 封装功耗）只覆盖整机的一部分：
主板、内存、硬盘、风扇、CPU 供电 VRM 的转换损耗以及电源自身的转换损耗都读不到。
这个模块把读不到的部分按可配置的参数补齐，并把"实测"和"估算"分开返回，
让界面可以如实区分两者，而不是把估算值伪装成实测值。

模型：

    cpu_socket_w = cpu_package_w / cpu_vrm_efficiency
    dc_w         = 实测功率 + (cpu_socket_w - cpu_package_w) + baseline_w
    total_w      = dc_w / psu_efficiency

其中 baseline_w 覆盖主板、芯片组、内存、硬盘和风扇；psu_efficiency 是电源的
交直流转换效率。两个参数都可以用墙插功率计读数校准（见 solve_calibration）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

MIN_CPU_VRM_EFFICIENCY = 0.50
MAX_CPU_VRM_EFFICIENCY = 1.0
MIN_PSU_EFFICIENCY = 0.50
MAX_PSU_EFFICIENCY = 0.99
MIN_BASELINE_W = 0.0
MAX_BASELINE_W = 500.0
MIN_PSU_RATED_W = 0.0
MAX_PSU_RATED_W = 5000.0
MAX_PLAUSIBLE_WALL_W = 5000.0

# 80 PLUS 曲线的形状：相对于额定负载 20%~80% 区间，轻载和满载的效率都要低一些。
_LOW_LOAD_RATIO = 0.20
_HIGH_LOAD_RATIO = 0.80
_LOW_LOAD_PENALTY = 0.96
_HIGH_LOAD_PENALTY = 0.98


class PowerModelError(ValueError):
    """校准输入不合理时抛出。"""


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class PowerModel:
    """一次快照使用的估算参数。"""

    enabled: bool = True
    cpu_vrm_efficiency: float = 0.89
    baseline_w: float = 75.0
    psu_efficiency: float = 0.90
    psu_rated_w: float = 0.0
    calibrated: bool = False

    @property
    def source(self) -> str:
        return "calibrated" if self.calibrated else "default"

    def efficiency_for(self, dc_w: float) -> float:
        """按负载率给出电源效率；额定功率未知时退回固定效率。"""
        if self.psu_rated_w <= 0 or dc_w <= 0:
            return self.psu_efficiency
        ratio = dc_w / self.psu_rated_w
        if ratio < _LOW_LOAD_RATIO:
            factor = _LOW_LOAD_PENALTY
        elif ratio > _HIGH_LOAD_RATIO:
            factor = _HIGH_LOAD_PENALTY
        else:
            factor = 1.0
        return max(MIN_PSU_EFFICIENCY, min(MAX_PSU_EFFICIENCY, self.psu_efficiency * factor))

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "cpu_vrm_efficiency": self.cpu_vrm_efficiency,
            "baseline_w": self.baseline_w,
            "psu_efficiency": self.psu_efficiency,
            "psu_rated_w": self.psu_rated_w or None,
            "calibrated": self.calibrated,
            "source": self.source,
        }


def accounted_dc_w(
    model: PowerModel, gpu_w: float | None, cpu_package_w: float | None,
    other_sensor_w: float | None,
) -> float | None:
    """实测功率加上 CPU 供电损耗，即"能解释的直流功率"，不含底噪和电源损耗。

    校准时用它作为自变量，保证和 estimate() 用的是同一个口径。
    """
    measured = measured_w(gpu_w, cpu_package_w, other_sensor_w)
    if measured is None:
        return None
    package = _finite(cpu_package_w)
    if package is None or package <= 0:
        return measured
    efficiency = min(max(model.cpu_vrm_efficiency, MIN_CPU_VRM_EFFICIENCY), MAX_CPU_VRM_EFFICIENCY)
    return measured + package / efficiency - package


def measured_w(
    gpu_w: float | None, cpu_package_w: float | None, other_sensor_w: float | None
) -> float | None:
    """真实传感器读数之和；一个都读不到时返回 None，不能伪造成 0。"""
    parts = [_finite(gpu_w), _finite(cpu_package_w), _finite(other_sensor_w)]
    present = [part for part in parts if part is not None]
    return sum(present) if present else None


def estimate(
    model: PowerModel, *, gpu_w: float | None = None, cpu_package_w: float | None = None,
    other_sensor_w: float | None = None,
) -> dict[str, Any]:
    """返回实测、估算与合计三个口径，以及估算项的明细。"""
    measured = measured_w(gpu_w, cpu_package_w, other_sensor_w)
    result: dict[str, Any] = {
        "measured_w": None if measured is None else round(measured, 2),
        "estimated_w": None,
        "total_w": None if measured is None else round(measured, 2),
        "gpu_w": None if _finite(gpu_w) is None else round(float(gpu_w), 2),
        "cpu_package_w": None if _finite(cpu_package_w) is None
        else round(float(cpu_package_w), 2),
        "sensor_w": None if _finite(other_sensor_w) is None
        else round(float(other_sensor_w), 2),
        "model": model.as_dict(),
        "estimate_breakdown": None,
    }
    if not model.enabled or measured is None:
        return result

    package = _finite(cpu_package_w) or 0.0
    cpu_vrm_loss = 0.0
    if package > 0:
        efficiency = min(
            max(model.cpu_vrm_efficiency, MIN_CPU_VRM_EFFICIENCY), MAX_CPU_VRM_EFFICIENCY
        )
        cpu_vrm_loss = package / efficiency - package
    baseline = max(0.0, model.baseline_w)
    dc_w = measured + cpu_vrm_loss + baseline
    psu_efficiency = model.efficiency_for(dc_w)
    total = dc_w / psu_efficiency
    psu_loss = total - dc_w

    result["estimated_w"] = round(total - measured, 2)
    result["total_w"] = round(total, 2)
    result["estimate_breakdown"] = {
        "cpu_vrm_loss_w": round(cpu_vrm_loss, 2),
        "baseline_w": round(baseline, 2),
        "psu_loss_w": round(psu_loss, 2),
        "psu_efficiency": round(psu_efficiency, 4),
        "dc_w": round(dc_w, 2),
    }
    return result


def solve_calibration(
    *, idle_accounted_w: float | None, idle_wall_w: float | None,
    load_accounted_w: float | None, load_wall_w: float | None,
    default_psu_efficiency: float,
) -> tuple[float, float]:
    """由墙插功率计读数反解 (baseline_w, psu_efficiency)。

    两个校准点（空闲与满载）可以同时解出底噪和电源效率：

        wall1 * eta = accounted1 + baseline
        wall2 * eta = accounted2 + baseline
        =>  eta = (accounted2 - accounted1) / (wall2 - wall1)

    只有一个点时电源效率沿用默认值，只解底噪。
    """
    idle = _calibration_point(idle_accounted_w, idle_wall_w, "空闲")
    load = _calibration_point(load_accounted_w, load_wall_w, "满载")
    if idle is None and load is None:
        raise PowerModelError("至少需要一个校准点")

    if idle is not None and load is not None:
        accounted_delta = load[0] - idle[0]
        wall_delta = load[1] - idle[1]
        if wall_delta <= 0 or accounted_delta <= 0:
            raise PowerModelError("满载校准点的实测功率和墙插功率都必须高于空闲校准点")
        efficiency = accounted_delta / wall_delta
        baseline = idle[1] * efficiency - idle[0]
    else:
        point = idle if idle is not None else load
        assert point is not None
        efficiency = default_psu_efficiency
        baseline = point[1] * efficiency - point[0]

    if not math.isfinite(efficiency) or not MIN_PSU_EFFICIENCY <= efficiency <= MAX_PSU_EFFICIENCY:
        raise PowerModelError(
            f"解出的电源效率 {efficiency:.3f} 不在 "
            f"{MIN_PSU_EFFICIENCY}~{MAX_PSU_EFFICIENCY} 之间，请核对墙插读数"
        )
    if not math.isfinite(baseline) or not MIN_BASELINE_W <= baseline <= MAX_BASELINE_W:
        raise PowerModelError(
            f"解出的固定底噪 {baseline:.1f} W 不在 "
            f"{MIN_BASELINE_W:.0f}~{MAX_BASELINE_W:.0f} W 之间，请核对墙插读数"
        )
    return round(baseline, 2), round(efficiency, 4)


def _calibration_point(
    accounted_w: float | None, wall_w: float | None, label: str
) -> tuple[float, float] | None:
    accounted = _finite(accounted_w)
    wall = _finite(wall_w)
    if accounted is None and wall is None:
        return None
    if accounted is None or wall is None:
        raise PowerModelError(f"{label}校准点需要同时提供实测功率和墙插功率")
    if wall <= 0 or wall > MAX_PLAUSIBLE_WALL_W:
        raise PowerModelError(f"{label}校准点的墙插功率必须在 0~{MAX_PLAUSIBLE_WALL_W:.0f} W 之间")
    if accounted < 0:
        raise PowerModelError(f"{label}校准点的实测功率不能为负")
    if wall < accounted:
        raise PowerModelError(f"{label}校准点的墙插功率不能低于已实测的 {accounted:.1f} W")
    return accounted, wall

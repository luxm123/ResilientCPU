"""
调度器统一接口

为不同调度策略提供统一接口，方便实验对比
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional
import asyncio

from .types import Function, Invocation, Machine, SchedulingDecision
from .baseline_scheduler import BaselineScheduler
from .jiagu_scheduler import JiaguLikeScheduler
from .scheduler import ResilientScheduler


class BaseScheduler(ABC):
    """调度器抽象基类"""

    @abstractmethod
    def register_function(self, function: Function) -> None:
        """注册函数"""
        pass

    @abstractmethod
    async def schedule(self, invocation: Invocation) -> Optional[SchedulingDecision]:
        """调度函数请求"""
        pass

    @abstractmethod
    async def restore_resources(self, function_id: str, machine_id: str) -> None:
        """释放资源"""
        pass

    @abstractmethod
    def get_statistics(self) -> dict:
        """获取统计"""
        pass


class SchedulerFactory:
    """调度器工厂"""

    SCHEDULERS = {
        "baseline": BaselineScheduler,
        "conservative": BaselineScheduler,
        "jiagu": JiaguLikeScheduler,
        "jiagu-like": JiaguLikeScheduler,
        "resilient": ResilientScheduler,
    }

    @classmethod
    def create(
        cls,
        scheduler_type: str,
        machines: List[Machine],
        **kwargs
    ) -> BaseScheduler:
        """
        创建调度器实例
        
        Args:
            scheduler_type: 调度器类型 ("baseline", "jiagu", "resilient")
            machines: 机器列表
            **kwargs: 其他参数
        """
        scheduler_type = scheduler_type.lower()
        
        if scheduler_type not in cls.SCHEDULERS:
            raise ValueError(f"Unknown scheduler type: {scheduler_type}")
        
        scheduler_class = cls.SCHEDULERS[scheduler_type]
        
        if scheduler_type == "baseline":
            return scheduler_class(
                machines=machines,
                cpu_utilization_threshold=kwargs.get("cpu_utilization_threshold", 0.7),
                min_available_cores=kwargs.get("min_available_cores", 1.0)
            )
        elif scheduler_type in ("jiagu", "jiagu-like"):
            return scheduler_class(
                machines=machines,
                model_path=kwargs.get("model_path"),
                capacity_update_interval=kwargs.get("capacity_update_interval", 60.0)
            )
        elif scheduler_type == "resilient":
            return scheduler_class(
                machines=machines,
                strategy="resilient",
                sensitivity_threshold=kwargs.get("sensitivity_threshold", 0.95),
                borrow_ratio=kwargs.get("borrow_ratio", 0.3),
                compensation_delay_ms=kwargs.get("compensation_delay_ms", 50.0)
            )
        else:
            return scheduler_class(machines=machines)

    @classmethod
    def available_schedulers(cls) -> List[str]:
        """获取可用的调度器列表"""
        return list(set(cls.SCHEDULERS.keys()))


# 导出
__all__ = [
    "BaseScheduler",
    "SchedulerFactory",
    "BaselineScheduler",
    "JiaguLikeScheduler",
    "ResilientScheduler"
]

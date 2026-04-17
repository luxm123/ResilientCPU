"""
Baseline 调度器 - 保守策略

Baseline策略特点：
1. 不超卖：每个函数独占资源，不共享
2. 不使用敏感度曲线
3. 不进行运行时补偿
4. 只放置在CPU利用率低的机器上
"""

import asyncio
import random
from typing import Dict, List, Optional
from collections import defaultdict

from .types import Function, Invocation, Machine, SchedulingDecision, FunctionState
from .cgroup_manager import CGroupManager


class BaselineScheduler:
    """
    保守调度器（Baseline）
    
    调度原则：
    - 资源隔离：每个函数独占分配的CPU核心
    - 避免争抢：只在CPU利用率低的机器上放置
    - 无超卖：总分配CPU数 <= 物理CPU数
    - 无补偿：不动态调整资源
    """

    def __init__(
        self,
        machines: List[Machine],
        cpu_utilization_threshold: float = 0.7,  # CPU利用率阈值
        min_available_cores: float = 1.0  # 最小可用核心数
    ):
        self.machines = {m.machine_id: m for m in machines}
        self.functions: Dict[str, Function] = {}
        self.cpu_utilization_threshold = cpu_utilization_threshold
        self.min_available_cores = min_available_cores

        # 统计
        self.scheduling_decisions: List[SchedulingDecision] = []
        self.rejected_invocations: List[Invocation] = []

        # cgroup管理器
        self.cgroup_mgr = CGroupManager()

    def register_function(self, function: Function) -> None:
        """注册函数"""
        self.functions[function.function_id] = function

    async def schedule(self, invocation: Invocation) -> Optional[SchedulingDecision]:
        """
        调度函数请求
        
        算法：
        1. 寻找满足条件的机器：
           - CPU利用率 < threshold (如70%)
           - 可用CPU核心 >= 函数需求
           - 总分配CPU数 + 函数需求 <= 物理CPU数
        2. 选择满足条件且负载最低的机器
        3. 放置函数
        """
        function = self.functions.get(invocation.function_id)
        if function is None:
            return None

        # 获取候选机器
        candidate_machines = self._get_candidate_machines(function)

        if not candidate_machines:
            self.rejected_invocations.append(invocation)
            return None

        # 选择负载最低的机器
        best_machine = None
        lowest_utilization = float('inf')

        for machine_id in candidate_machines:
            machine = self.machines[machine_id]
            if machine.cpu_utilization < lowest_utilization:
                lowest_utilization = machine.cpu_utilization
                best_machine = machine_id

        if best_machine is None:
            self.rejected_invocations.append(invocation)
            return None

        # 预留资源
        self._reserve_resources(function, best_machine)

        # 创建调度决策
        machine = self.machines[best_machine]
        contention = self._estimate_contention(best_machine)

        decision = SchedulingDecision(
            invocation_id=invocation.invocation_id,
            function_id=invocation.function_id,
            selected_machine=best_machine,
            scheduling_time=invocation.arrival_time,
            contention_estimate=contention,
            sensitivity_used=False,
            predicted_degradation=0.0,
            confidence=1.0
        )

        self.scheduling_decisions.append(decision)

        return decision

    def _get_candidate_machines(self, function: Function) -> List[str]:
        """获取候选机器列表"""
        candidates = []
        required_cores = function.cpu_cores

        for machine_id, machine in self.machines.items():
            # 检查CPU利用率
            if machine.cpu_utilization >= self.cpu_utilization_threshold:
                continue

            # 检查可用核心数
            allocated_cores = machine.total_allocated_cores()
            available_cores = machine.cpu_cores - allocated_cores

            if available_cores < required_cores:
                continue

            # 检查：放置后总分配是否超过物理核心（无超卖）
            if allocated_cores + required_cores > machine.cpu_cores:
                continue

            candidates.append(machine_id)

        return candidates

    def _estimate_contention(self, machine_id: str) -> float:
        """估算CPU争抢程度（Baseline应该很低）"""
        machine = self.machines[machine_id]
        allocated = machine.total_allocated_cores()
        if allocated <= machine.cpu_cores:
            return 0.0
        # 理论上Baseline不应该超卖，但可能有临时波动
        return (allocated - machine.cpu_cores) / machine.cpu_cores * 100

    def _reserve_resources(self, function: Function, machine_id: str) -> None:
        """预留资源"""
        machine = self.machines[machine_id]
        shares = self._cores_to_shares(function.cpu_cores)
        machine.total_cpu_shares_allocated += shares
        machine.running_functions[function.function_id] = function

    def _cores_to_shares(self, cores: float) -> int:
        """CPU核心数转换为shares"""
        return int(cores * 100)

    async def restore_resources(self, function_id: str, machine_id: str) -> None:
        """函数完成后恢复资源"""
        machine = self.machines[machine_id]
        function = machine.running_functions.get(function_id)

        if function:
            shares = self._cores_to_shares(function.cpu_cores)
            machine.total_cpu_shares_allocated -= shares
            del machine.running_functions[function_id]

    def get_statistics(self) -> dict:
        """获取统计信息"""
        total = len(self.scheduling_decisions) + len(self.rejected_invocations)
        return {
            "total_invocations": total,
            "accepted": len(self.scheduling_decisions),
            "rejected": len(self.rejected_invocations),
            "acceptance_rate": len(self.scheduling_decisions) / total if total > 0 else 0,
            "scheduler_type": "Baseline (Conservative)"
        }


# 为了兼容性，提供别名
ConservativeScheduler = BaselineScheduler


if __name__ == "__main__":
    # 测试
    from .types import Machine, Function

    machines = [
        Machine("m0", 8, 32*1024),
        Machine("m1", 8, 32*1024),
    ]

    scheduler = BaselineScheduler(machines)

    func = Function("f1", "TestFunc", 512, 1.0, 1.0)
    scheduler.register_function(func)

    # 模拟调度
    from .types import Invocation
    import time

    inv = Invocation("inv_1", "f1", time.time(), None, None)
    decision = asyncio.run(scheduler.schedule(inv))

    if decision:
        print(f"调度成功: {decision.selected_machine}")
    else:
        print("调度失败")

    print(f"统计: {scheduler.get_statistics()}")

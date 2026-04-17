"""
基于敏感度的调度器 - Resilient Scheduler

核心调度策略：
1. 为每个函数维护敏感度曲线
2. 调度时检查目标机器的当前CPU争抢程度
3. 仅当争抢程度在函数的可接受范围内时才调度
4. 实现更激进的超卖（允许在拐点内超卖）
5. 运行时通过调节cgroup shares快速补偿，而非迁移
"""

import asyncio
import random
import numpy as np
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import heapq

from .types import (
    Function, Invocation, Machine, SchedulingDecision,
    FunctionState, SensitivityProfile
)
from .cgroup_manager import CGroupManager
from .monitor import Monitor


class ResilientScheduler:
    """
    ResilientCPU调度器
    
    调度原则：
    - 不预测干扰，不避免干扰，而是容忍干扰
    - 利用敏感度曲线进行风险感知调度
    - 在可接受风险范围内激进超卖
    - 运行时通过细粒度资源调节而非实例迁移来保障SLA
    """

    def __init__(
        self,
        machines: List[Machine],
        strategy: str = "resilient",
        sensitivity_threshold: float = 0.95,
        borrow_ratio: float = 0.3,
        compensation_delay_ms: float = 50.0
    ):
        """
        Args:
            machines: 机器列表
            strategy: 调度策略 ("resilient" | "conservative" | "aggressive")
            sensitivity_threshold: 使用敏感度曲线的置信度阈值
            borrow_ratio: 可从低敏感度函数借用的CPU比例
            compensation_delay_ms: 补偿延迟阈值（毫秒）
        """
        self.machines = {m.machine_id: m for m in machines}
        self.functions: Dict[str, Function] = {}
        self.strategy = strategy
        self.sensitivity_threshold = sensitivity_threshold
        self.borrow_ratio = borrow_ratio
        self.compensation_delay = compensation_delay_ms

        # 调度统计
        self.scheduling_decisions: List[SchedulingDecision] = []
        self.rejected_invocations: List[Invocation] = []
        self.compensation_actions: List[dict] = []

        # cgroup管理器
        self.cgroup_mgr = CGroupManager()

        # 监控器引用（用于获取实时CPU争抢程度）
        self.monitor: Optional[Monitor] = None

    def set_monitor(self, monitor: Monitor) -> None:
        """关联监控器"""
        self.monitor = monitor

    def register_function(self, function: Function) -> None:
        """注册函数及其敏感度曲线"""
        self.functions[function.function_id] = function

    async def schedule(self, invocation: Invocation) -> Optional[SchedulingDecision]:
        """
        调度一个函数调用请求

        Returns:
            SchedulingDecision 或 None（如果无法调度）
        """
        function = self.functions.get(invocation.function_id)
        if function is None:
            logger.error(f"函数未注册: {invocation.function_id}")
            return None

        # 策略选择
        if self.strategy == "resilient":
            selected_machine = await self._resilient_schedule(invocation, function)
        elif self.strategy == "conservative":
            selected_machine = await self._conservative_schedule(invocation, function)
        elif self.strategy == "aggressive":
            selected_machine = await self._aggressive_schedule(invocation, function)
        else:
            selected_machine = await self._resilient_schedule(invocation, function)

        if selected_machine is None:
            self.rejected_invocations.append(invocation)
            return None

        # 创建调度决策
        machine = self.machines[selected_machine]

        # 估计CPU争抢程度（如果没有监控器，使用历史估计）
        contention_estimate = self._estimate_contention(selected_machine)

        # 预测性能退化
        predicted_degradation = 0.0
        if function.sensitivity_profile:
            predicted_degradation = function.sensitivity_profile.get_performance_degradation(
                contention_estimate
            )

        decision = SchedulingDecision(
            invocation_id=invocation.invocation_id,
            function_id=invocation.function_id,
            selected_machine=selected_machine,
            scheduling_time=invocation.arrival_time,
            contention_estimate=contention_estimate,
            sensitivity_used=(function.sensitivity_profile is not None),
            predicted_degradation=predicted_degradation,
            confidence=self.sensitivity_threshold
        )

        self.scheduling_decisions.append(decision)

        # 预分配资源（预留CPU shares）
        await self._reserve_resources(function, selected_machine)

        return decision

    async def _resilient_schedule(
        self,
        invocation: Invocation,
        function: Function
    ) -> Optional[str]:
        """
        Resilient策略：基于敏感度曲线的风险感知调度
        
        算法：
        1. 获取所有候选机器
        2. 对每台机器估计当前/预测CPU争抢程度
        3. 如果函数有敏感度曲线：
           - 检查争抢程度是否在可接受范围内（<= 拐点）
           - 如果在，则接受该机器
        4. 如果没有敏感度曲线，使用传统启发式
        5. 选择"最安全"的机器（争抢程度最低且满足约束）
        """
        candidate_machines = self._get_candidate_machines(function)

        if not candidate_machines:
            return None

        best_machine = None
        best_score = float('-inf')

        for machine_id in candidate_machines:
            machine = self.machines[machine_id]

            # 检查资源容量
            if not self._check_resource_capacity(function, machine):
                continue

            # 估计当前争抢程度
            contention = self._estimate_contention(machine_id)

            # 使用敏感度曲线评估风险
            risk_score = 1.0  # 默认低风险
            if function.sensitivity_profile:
                # 检查是否可接受
                is_acceptable = function.sensitivity_profile.is_acceptable_contention(contention)

                if is_acceptable:
                    # 距离拐点的安全裕度
                    max_acceptable = function.sensitivity_profile.get_max_acceptable_contention()
                    safety_margin = max(0, max_acceptable - contention) / max_acceptable
                    risk_score = 0.8 + 0.2 * safety_margin  # 0.8-1.0
                else:
                    # 超过可接受范围，风险很高
                    risk_score = 0.0

            # 考虑资源利用率（倾向于使用利用率适中的机器，避免热点）
            utilization_penalty = machine.cpu_utilization * 0.3

            # 综合评分
            score = risk_score - utilization_penalty

            if score > best_score:
                best_score = score
                best_machine = machine_id

        return best_machine if best_score > 0 else None

    async def _conservative_schedule(
        self,
        invocation: Invocation,
        function: Function
    ) -> Optional[str]:
        """
        保守策略：严格避免任何风险
        
        - 只考虑CPU争抢程度很低的机器（<20%）
        - 必须满足敏感度曲线的严格要求
        - 预留更多资源余量
        """
        candidate_machines = self._get_candidate_machines(function, conservative=True)

        best_machine = None
        lowest_contention = float('inf')

        for machine_id in candidate_machines:
            machine = self.machines[machine_id]

            if not self._check_resource_capacity(function, machine, buffer=0.3):
                continue

            contention = self._estimate_contention(machine_id)

            # 严格检查
            if function.sensitivity_profile:
                if not function.sensitivity_profile.is_acceptable_contention(contention):
                    continue
                # 额外的安全裕度
                max_acceptable = function.sensitivity_profile.get_max_acceptable_contention()
                if contention > max_acceptable * 0.7:
                    continue

            if contention < lowest_contention:
                lowest_contention = contention
                best_machine = machine_id

        return best_machine

    async def _aggressive_schedule(
        self,
        invocation: Invocation,
        function: Function
    ) -> Optional[str]:
        """
        激进策略：最大化资源利用率
        
        - 允许在拐点附近调度（但不超过）
        - 更激进的超卖
        - 依赖运行时补偿机制
        """
        candidate_machines = list(self.machines.keys())

        best_machine = None
        best_utilization = -1.0

        for machine_id in candidate_machines:
            machine = self.machines[machine_id]

            # 检查基本容量（不预留缓冲）
            if not self._check_resource_capacity(function, machine, buffer=0.0):
                continue

            contention = self._estimate_contention(machine_id)

            if function.sensitivity_profile:
                # 允许在拐点附近（最多到拐点）
                knee = function.sensitivity_profile.knee_point
                if contention > knee:
                    continue
                # 评分：越接近拐点越好（资源利用率高）
                score = contention / knee if knee > 0 else 1.0
            else:
                # 没有敏感度曲线，按利用率评分
                score = machine.cpu_utilization

            if score > best_utilization:
                best_utilization = score
                best_machine = machine_id

        return best_machine

    def _get_candidate_machines(
        self,
        function: Function,
        conservative: bool = False
    ) -> List[str]:
        """
        获取候选机器列表
        
        过滤条件：
        1. 机器健康状态正常
        2. 有足够的CPU shares容量
        3. 满足亲和性/反亲和性约束（可选）
        """
        candidates = []

        for machine_id, machine in self.machines.items():
            # 基本过滤
            if machine.cpu_utilization > 0.95:  # CPU使用率过高
                continue

            if conservative:
                # 保守策略要求更严格
                if machine.cpu_utilization > 0.7:
                    continue

            candidates.append(machine_id)

        return candidates

    def _check_resource_capacity(
        self,
        function: Function,
        machine: Machine,
        buffer: float = 0.1
    ) -> bool:
        """
        检查机器是否有足够资源
        
        Args:
            buffer: 资源缓冲比例（保留buffer%的资源余量）
        """
        # 计算函数需���的CPU shares
        required_shares = self._cores_to_shares(function.cpu_cores)

        # 考虑buffer
        effective_available = machine.available_cpu_shares() * (1 - buffer)

        return effective_available >= required_shares

    def _estimate_contention(self, machine_id: str) -> float:
        """
        估计机器的CPU争抢程度（0-100%）
        
        数据来源：
        1. 如果有监控器，使用实时测量值
        2. 否则使用历史统计数据
        3. 如果都没有，基于已分配资源估算
        """
        machine = self.machines[machine_id]

        if self.monitor:
            # 从监控器获取实时争抢程度
            return self.monitor.get_contention_level(machine_id)

        # 基于已分配资源估算
        # 假设每个函数占用一定CPU，争抢 = (总分配 - 物理核心) / 总分配
        allocated_cores = machine.total_allocated_cores()
        physical_cores = machine.cpu_cores

        if allocated_cores <= physical_cores:
            return 0.0

        contention = (allocated_cores - physical_cores) / allocated_cores * 100
        return min(100.0, max(0.0, contention))

    def _reserve_resources(self, function: Function, machine_id: str) -> None:
        """预留资源（更新机器状态）"""
        machine = self.machines[machine_id]
        shares = self._cores_to_shares(function.cpu_cores)
        machine.total_cpu_shares_allocated += shares
        machine.running_functions[function.function_id] = function

    def _cores_to_shares(self, cores: float) -> int:
        """CPU核心数转换为shares（与cgroup_manager保持一致）"""
        return int(cores * 100)  # 1核 = 100 shares (v2 weight)

    async def compensate_sla_violation(
        self,
        machine_id: str,
        function_id: str,
        violation_delay_ms: float
    ) -> bool:
        """
        SLA违规补偿机制

        当检测到函数延迟超标时：
        1. 立即增加该函数的CPU shares（优先级提升）
        2. 从同机器上低敏感度函数"借用"CPU资源
        3. 补偿完成后逐步恢复

        Returns:
            补偿是否成功
        """
        if violation_delay_ms <= self.compensation_delay:
            return True  # 轻微波动，无需补偿

        machine = self.machines[machine_id]
        victim_function = None
        victim_sensitivity = 1.0

        # 寻找"可借用"的低敏感度函数
        for fid, func in machine.running_functions.items():
            if fid == function_id:
                continue

            if func.sensitivity_profile:
                # 低敏感度：在较高争抢下仍能保持性能
                max_acceptable = func.sensitivity_profile.get_max_acceptable_contention()
                sensitivity = 1.0 - max_acceptable / 100.0  # 值越小越不敏感
            else:
                # 没有敏感度信息，假设敏感度高（不借用）
                sensitivity = 1.0

            if sensitivity < victim_sensitivity:
                victim_sensitivity = sensitivity
                victim_function = func

        if not victim_function:
            logger.warning(f"没有可借用的低敏感度函数，无法补偿")
            return False

        # 执行补偿：从victim转移CPU shares给违规函数
        compensation_shares = int(self._cores_to_shares(function.cpu_cores) * self.borrow_ratio)

        # 增加受影响函数的shares
        success1 = self.cgroup_mgr.set_cpu_shares(
            function_id,
            self._cores_to_shares(function.cpu_cores) + compensation_shares
        )

        # 减少victim函数的shares
        success2 = self.cgroup_mgr.set_cpu_shares(
            victim_function.function_id,
            self._cores_to_shares(victim_function.cpu_cores) - compensation_shares
        )

        if success1 and success2:
            logger.info(
                f"补偿触发: 从 {victim_function.name} 借用 {compensation_shares} shares "
                f"给 {self.functions[function_id].name}"
            )
            self.compensation_actions.append({
                "timestamp": asyncio.get_event_loop().time(),
                "machine": machine_id,
                "donor": victim_function.function_id,
                "receiver": function_id,
                "shares": compensation_shares
            })
            return True

        return False

    async def restore_resources(
        self,
        function_id: str,
        machine_id: str
    ) -> None:
        """函数执行完成后恢复资源"""
        machine = self.machines[machine_id]
        function = machine.running_functions.get(function_id)

        if function:
            # 恢复CPU shares到正常值
            normal_shares = self._cores_to_shares(function.cpu_cores)
            self.cgroup_mgr.set_cpu_shares(function_id, normal_shares)

            # 更新机器状态
            machine.total_cpu_shares_allocated -= normal_shares
            del machine.running_functions[function_id]

    def get_machine_utilization(self, machine_id: str) -> float:
        """获取机器资源利用率"""
        machine = self.machines.get(machine_id)
        if not machine:
            return 0.0
        return machine.cpu_utilization

    def get_statistics(self) -> dict:
        """获取调度器统计信息"""
        total = len(self.scheduling_decisions)
        accepted = sum(1 for d in self.scheduling_decisions if d.selected_machine)
        rejected = len(self.rejected_invocations)

        return {
            "total_invocations": total,
            "accepted": accepted,
            "rejected": rejected,
            "acceptance_rate": accepted / total if total > 0 else 0,
            "compensation_count": len(self.compensation_actions),
            "avg_predicted_degradation": np.mean([
                d.predicted_degradation for d in self.scheduling_decisions
            ]) if self.scheduling_decisions else 0
        }


import logging
logger = logging.getLogger(__name__)

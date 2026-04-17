"""
自适应调节器 - Adaptive Regulator

运行时的自动调节机制：
1. 接收Monitor的SLA违规告警
2. 通过调整cgroup CPU shares进行毫秒级补偿
3. 优先从低敏感度函数"借用"资源
4. 渐进式调节，避免震荡
"""

import asyncio
import time
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import numpy as np

from .types import Function, Machine, SensitivityProfile
from .monitor import Monitor, Alert
from .cgroup_manager import CGroupManager
from .scheduler import ResilientScheduler
import logging

logger = logging.getLogger(__name__)


class AdaptiveRegulator:
    """
    自适应调节器
    
    核心思想：
    - 不迁移实例（太慢）
    - 而是调整cgroup资源配额
    - 优先从"不敏感"函数借用资源
    - 实现毫秒级的SLA恢复
    
    调节策略：
    1. 检测到SLA违规后：
       - 检查违规严重程度
       - 决定是否需要补偿
    2. 补偿决策：
       - 找出所有可"牺牲"的低敏感度函数
       - 计算需要的CPU shares
       - 执行转移
    3. 恢复策略：
       - 补偿后观察效果
       - 逐步恢复正常资源分配
       - 避免过度调节
    """

    def __init__(
        self,
        machines: Dict[str, Machine],
        scheduler: ResilientScheduler,
        monitor: Monitor,
        max_compensation_ratio: float = 0.3,  # 最多借用30%的CPU资源
        recovery_interval: float = 5.0,        # 每5秒尝试恢复
        min_violation_threshold: float = 1.2    # 违规超过1.2倍才触发
    ):
        self.machines = machines
        self.scheduler = scheduler
        self.monitor = monitor
        self.max_compensation_ratio = max_compensation_ratio
        self.recovery_interval = recovery_interval
        self.min_violation_threshold = min_violation_threshold

        # cgroup管理器
        self.cgroup_mgr = CGroupManager()

        # 调节状态
        self.active_compensations: Dict[str, dict] = {}  # function_id -> compensation_info
        self.regulator_enabled = True

        # 统计
        self.compensation_history: List[dict] = []
        self.total_compensations = 0
        self.successful_compensations = 0

        # 恢复任务
        self.recovery_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """启动调节器"""
        # 设置Monitor回调
        self.monitor.callback = self._handle_alert

        # 启动恢复循环
        self.recovery_task = asyncio.create_task(self._recovery_loop())
        print("[Regulator] 自适应调节器已启动")

    async def stop(self) -> None:
        """停止调节器"""
        self.regulator_enabled = False
        if self.recovery_task:
            self.recovery_task.cancel()
            try:
                await self.recovery_task
            except asyncio.CancelledError:
                pass

        # 清理所有补偿，恢复原始分配
        await self._restore_all()
        print("[Regulator] 自适应调节器已停止")

    async def _handle_alert(self, alert: Alert) -> None:
        """
        处理SLA违规告警
        
        Args:
            alert: SLA违规告警对象
        """
        if not self.regulator_enabled:
            return

        logger.info(
            f"[Regulator] 收到SLA告警: {alert.function_id} "
            f"violation={alert.violation_factor:.2f}x"
        )

        # 检查是否需要补偿
        if alert.violation_factor < self.min_violation_threshold:
            logger.debug(f"违规轻微，跳过: {alert.violation_factor:.2f}x < {self.min_violation_threshold}")
            return

        # 执行补偿
        success = await self._compensate(
            alert.machine_id,
            alert.function_id,
            alert.violation_factor
        )

        if success:
            self.successful_compensations += 1
        self.total_compensations += 1

    async def _compensate(
        self,
        machine_id: str,
        function_id: str,
        violation_factor: float
    ) -> bool:
        """
        执行补偿
        
        核心算法：
        1. 确定需要的额外CPU shares
        2. 找出可借用的低敏感度函数
        3. 执行资源转移
        """
        machine = self.machines.get(machine_id)
        if not machine:
            return False

        victim_function = self._find_best_victim(machine, function_id)
        if not victim_function:
            logger.warning(f"[Regulator] 无法找到合适的借源函数")
            return False

        # 计算需要的shares
        victim_func = machine.running_functions[victim_function]
        base_shares = self._cores_to_shares(victim_func.cpu_cores)

        # 借用比例与违规程度成正比，但不超过上限
        borrow_ratio = min(
            self.max_compensation_ratio,
            (violation_factor - 1.0) / 2.0  # 1.2x违规 -> 10%, 2.0x违规 -> 50% (cap at 30%)
        )
        borrow_shares = int(base_shares * borrow_ratio)

        if borrow_shares < 10:  # 最小调节量
            logger.debug("借用shares太小，跳过")
            return False

        # 执行补偿
        donor_shares = base_shares - borrow_shares
        receiver_new_shares = base_shares + borrow_shares

        # 获取接收函数
        receiver_func = machine.running_functions.get(function_id)
        if receiver_func:
            receiver_current_shares = self._cores_to_shares(receiver_func.cpu_cores)
            receiver_new_shares = receiver_current_shares + borrow_shares

            # 调整cgroup
            success1 = self.cgroup_mgr.set_cpu_shares(function_id, receiver_new_shares)
            success2 = self.cgroup_mgr.set_cpu_shares(victim_function, donor_shares)

            if success1 and success2:
                # 记录补偿状态
                self.active_compensations[function_id] = {
                    "machine_id": machine_id,
                    "donor": victim_function,
                    "additional_shares": borrow_shares,
                    "timestamp": time.time(),
                    "original_donor_shares": base_shares,
                    "restored": False
                }

                logger.info(
                    f"[Regulator] ✅ 补偿成功: "
                    f"从 {victim_function} 借用 {borrow_shares} shares -> {function_id}"
                )

                self.compensation_history.append({
                    "timestamp": time.time(),
                    "machine": machine_id,
                    "receiver": function_id,
                    "donor": victim_function,
                    "shares": borrow_shares,
                    "violation_factor": violation_factor
                })
                return True

        return False

    def _find_best_victim(
        self,
        machine: Machine,
        protected_function_id: str
    ) -> Optional[str]:
        """
        找到最佳"牺牲"函数
        
        选择标准：
        1. 有低敏感度（在争抢下性能退化慢）
        2. 当前CPU shares足够大（可以借出）
        3. 不是被保护的目标函数
        """
        best_victim = None
        best_score = -float('inf')  # 越低越适合被牺牲

        for function_id, func in machine.running_functions.items():
            if function_id == protected_function_id:
                continue

            # 计算"可牺牲性"分数
            score = self._calculate_sacrifice_score(func)

            if score > best_score:
                best_score = score
                best_victim = function_id

        return best_victim

    def _calculate_sacrifice_score(self, function: Function) -> float:
        """
        计算函数被牺牲的适合度
        
        分数越高，越适合被牺牲（借用资源）
        """
        if function.sensitivity_profile:
            # 低敏感度 = 在争抢下性能退化慢 = 适合被牺牲
            max_acceptable = function.sensitivity_profile.get_max_acceptable_contention()
            # 归一化：0-100 -> 0-1，越大越不敏感
            insensitivity = max_acceptable / 100.0
            return insensitivity
        else:
            # 没有敏感度信息，默认中等敏感度
            return 0.5

    async def _recovery_loop(self) -> None:
        """恢复循环：逐步恢复正常资源分配"""
        while self.regulator_enabled:
            await asyncio.sleep(self.recovery_interval)

            if not self.regulator_enabled:
                break

            await self._restore_expired_compensations()

    async def _restore_expired_compensations(self) -> None:
        """
        恢复过期的补偿
        
        策略：
        1. 检查补偿是否已经生效（SLA恢复）
        2. 如果持续稳定，逐步减少借用量
        3. 最终完全恢复
        """
        to_restore = []

        for function_id, comp_info in self.active_compensations.items():
            if comp_info.get("restored", False):
                continue

            # 检查是否需要继续补偿
            latency_stats = self.monitor.get_function_latency_stats(function_id)
            if latency_stats:
                p95 = latency_stats.get("p95", float('inf'))
                machine_id = comp_info["machine_id"]
                machine = self.machines.get(machine_id)
                if machine:
                    function = machine.running_functions.get(function_id)
                    if function:
                        sla_target = function.timeout * 1000 * 0.8
                        # 如果P95延迟已经低于SLA目标，可以开始恢复
                        if p95 < sla_target * 1.1:  # 10%余量
                            to_restore.append(function_id)

        # 执行恢复
        for function_id in to_restore:
            await self._restore_compensation(function_id)

    async def _restore_compensation(self, function_id: str) -> None:
        """恢复单个函数的补偿"""
        comp_info = self.active_compensations.get(function_id)
        if not comp_info or comp_info.get("restored"):
            return

        machine_id = comp_info["machine_id"]
        donor = comp_info["donor"]
        additional_shares = comp_info["additional_shares"]

        # 获取当前shares
        machine = self.machines.get(machine_id)
        if not machine:
            return

        # 恢复受保护函数的shares
        protected_func = machine.running_functions.get(function_id)
        if protected_func:
            current_shares = self._cores_to_shares(protected_func.cpu_cores)
            # 包含借来的shares
            protected_new_shares = current_shares - additional_shares
            if protected_new_shares > 0:
                self.cgroup_mgr.set_cpu_shares(function_id, protected_new_shares)

        # 恢复捐赠函数的shares
        donor_func = machine.running_functions.get(donor)
        if donor_func:
            self.cgroup_mgr.set_cpu_shares(donor, comp_info["original_donor_shares"])

        # 标记为已恢复
        comp_info["restored"] = True
        comp_info["restore_timestamp"] = time.time()

        logger.info(
            f"[Regulator] 恢复资源分配: {function_id} shares正常化"
        )

    async def _restore_all(self) -> None:
        """恢复所有补偿（关闭时调用）"""
        for function_id in list(self.active_compensations.keys()):
            await self._restore_compensation(function_id)

    def _cores_to_shares(self, cores: float) -> int:
        """CPU核心数转换为shares"""
        return int(cores * 100)

    def get_statistics(self) -> dict:
        """获取调节器统计"""
        return {
            "total_compensations": self.total_compensations,
            "successful_compensations": self.successful_compensations,
            "success_rate": (
                self.successful_compensations / self.total_compensations
                if self.total_compensations > 0 else 0
            ),
            "active_compensations": len(
                [c for c in self.active_compensations.values()
                 if not c.get("restored")]
            )
        }


# 测试代码
async def main():
    """测试调节器"""
    from .types import Machine, Function, SensitivityProfile

    # 创建测试环境
    machines = {
        "machine_0": Machine(
            machine_id="machine_0",
            cpu_cores=8,
            memory_mb=32*1024
        )
    }

    from .scheduler import ResilientScheduler
    scheduler = ResilientScheduler(list(machines.values()))

    monitor = Monitor(machines)
    await monitor.start()

    regulator = AdaptiveRegulator(machines, scheduler, monitor)

    # 添加测试函数
    machine = machines["machine_0"]
    machine.running_functions["high_sensitivity_func"] = Function(
        function_id="high_sensitivity_func",
        name="HighSensitivity",
        memory_mb=512,
        cpu_cores=1.0,
        execution_time_mean=1.0,
        sensitivity_profile=SensitivityProfile(
            function_id="high_sensitivity_func",
            sensitivity_points=[(0, 0), (20, 0.1), (40, 0.3), (60, 0.6), (80, 0.9), (100, 1.0)],
            knee_point=40.0,
            acceptable_degradation=0.05
        )
    )

    machine.running_functions["low_sensitivity_func"] = Function(
        function_id="low_sensitivity_func",
        name="LowSensitivity",
        memory_mb=512,
        cpu_cores=1.0,
        execution_time_mean=1.0,
        sensitivity_profile=SensitivityProfile(
            function_id="low_sensitivity_func",
            sensitivity_points=[(0, 0), (20, 0.02), (40, 0.05), (60, 0.1), (80, 0.2), (100, 0.4)],
            knee_point=70.0,
            acceptable_degradation=0.05
        )
    )

    await regulator.start()

    # 模拟告警
    from .monitor import Alert
    fake_alert = Alert(
        timestamp=time.time(),
        machine_id="machine_0",
        function_id="high_sensitivity_func",
        invocation_id="test",
        measured_latency_ms=2500.0,
        sla_target_ms=800.0,
        violation_factor=3.125,
        severity="critical"
    )

    await regulator._handle_alert(fake_alert)

    await asyncio.sleep(2)

    stats = regulator.get_statistics()
    print(f"调节器统计: {stats}")

    await regulator.stop()
    await monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())

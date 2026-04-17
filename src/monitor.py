"""
运行时监控器 - Monitor

实时监控每个函数实例的延迟和资源使用情况：
1. 在毫秒级粒度采样延迟（通过eBPF或应用内探针）
2. 检测SLA违规
3. 触发补偿机制
4. 更新CPU争抢程度估计
"""

import asyncio
import time
import psutil
from typing import Dict, List, Optional, Callable
from collections import deque
from dataclasses import dataclass, field
import numpy as np

from .types import MonitoringSample, Machine, Function, Invocation
from .cgroup_manager import CGroupManager


@dataclass
class Alert:
    """SLA违规告警"""
    timestamp: float
    machine_id: str
    function_id: str
    invocation_id: str
    measured_latency_ms: float
    sla_target_ms: float
    violation_factor: float  # 超标倍数
    severity: str  # "warning" | "critical"


class Monitor:
    """
    运行时监控器
    
    核心功能：
    1. 采样：定期从cgroup和函数运行时收集指标
    2. 检测：识别延迟超标和资源争抢
    3. 告警：触发SLA违规事件
    4. 估计：更新机器级CPU争抢程度
    """

    def __init__(
        self,
        machines: Dict[str, Machine],
        sample_interval: float = 0.1,  # 100ms采样
        window_size: int = 10,         # 滑动窗口大小
        alert_threshold: float = 1.5,  # 延迟超标1.5倍触发告警
        callback: Optional[Callable[[Alert], None]] = None
    ):
        self.machines = machines
        self.sample_interval = sample_interval
        self.window_size = window_size
        self.alert_threshold = alert_threshold
        self.callback = callback

        # 监控数据存储
        self.samples: Dict[str, deque] = {}  # machine_id -> deque[MonitoringSample]
        self.latency_windows: Dict[str, deque] = {}  # function_id -> deque[latency_ms]

        # 实时CPU争抢程度估计
        self.contention_levels: Dict[str, float] = {}

        # 运行状态
        self.running = False
        self.monitor_task: Optional[asyncio.Task] = None

        # cgroup管理器
        self.cgroup_mgr = CGroupManager()

    async def start(self) -> None:
        """启动监控循环"""
        self.running = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        print("[Monitor] 监控器已启动")

    async def stop(self) -> None:
        """停止监控"""
        self.running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        print("[Monitor] 监控器已停止")

    async def _monitor_loop(self) -> None:
        """主监控循环"""
        while self.running:
            try:
                await self._sample_all()
                await asyncio.sleep(self.sample_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Monitor] 监控循环错误: {e}")

    async def _sample_all(self) -> None:
        """采样所有机器和函数"""
        current_time = time.time()

        for machine_id, machine in self.machines.items():
            # 采样机器级指标
            machine_sample = await self._sample_machine(machine_id, machine, current_time)

            # 更新机器争抢程度估���
            self._update_contention_estimate(machine_id, machine_sample)

            # 采样每个函数的延迟
            for function_id, function in machine.running_functions.items():
                latency = await self._measure_function_latency(function_id, machine_id)
                if latency is not None:
                    await self._check_sla_violation(
                        machine_id, function_id, latency, function
                    )

    async def _sample_machine(
        self,
        machine_id: str,
        machine: Machine,
        timestamp: float
    ) -> MonitoringSample:
        """
        采样机器级指标
        
        数据来源：
        1. /proc/stat - CPU使用率
        2. cgroup - 分配的CPU shares和实际使用
        3. 内存使用
        """
        # 读取系统CPU使用率
        cpu_percent = psutil.cpu_percent(interval=None) / 100.0

        # 获取cgroup统计
        cgroup_stats = {}
        for function_id in machine.running_functions.keys():
            stats = self.cgroup_mgr.get_cpu_stats(function_id)
            cgroup_stats[function_id] = stats

        # 估计CPU争抢程度
        # 方法：比较总分配的shares与物理核心，结合实际使用率
        total_shares = machine.total_cpu_shares_allocated
        contention = 0.0
        if total_shares > machine.cpu_cores * 100:  # 超卖阈值
            # 超卖比例
            oversubscription = (total_shares / (machine.cpu_cores * 100)) - 1.0
            # 结合实际CPU使用率调整
            contention = min(100.0, oversubscription * 100 * cpu_percent)

        sample = MonitoringSample(
            timestamp=timestamp,
            machine_id=machine_id,
            function_id=None,
            cpu_usage=cpu_percent,
            memory_usage=machine.memory_utilization,
            cpu_shares=total_shares,
            latency_ms=None
        )

        # 存储样本
        if machine_id not in self.samples:
            self.samples[machine_id] = deque(maxlen=self.window_size * 10)
        self.samples[machine_id].append(sample)

        # 更新机器状态
        machine.cpu_utilization = cpu_percent
        machine.cpu_contention = contention

        return sample

    async def _measure_function_latency(
        self,
        function_id: str,
        machine_id: str
    ) -> Optional[float]:
        """
        测量函数实例的延迟
        
        实现方式：
        1. 从cgroup中读取函数的执行统计
        2. 或者通过应用内探针上报
        3. 或者通过eBPF跟踪函数执行时间
        
        这里采用模拟方式：从invocation历史记录计算
        """
        # 在实际系统中，这里会：
        # - 从eBPF map读取延迟直方图
        # - 或从函数日志中提取
        # - 或从cgroup的cpu.stat计算执行时间

        # 模拟：返回一个基于当前争抢程度的延迟值
        machine = self.machines[machine_id]
        contention = machine.cpu_contention

        # 模拟延迟增长：基线延迟 * (1 + contention * sensitivity_factor)
        function = machine.running_functions.get(function_id)
        if function:
            baseline = function.execution_time_mean * 1000  # 转换为ms
            sensitivity_factor = 0.5  # 敏感度系数
            latency = baseline * (1 + contention / 100 * sensitivity_factor)

            # 添加随机波动
            latency *= random.uniform(0.9, 1.1)

            return latency

        return None

    async def _check_sla_violation(
        self,
        machine_id: str,
        function_id: str,
        latency_ms: float,
        function: Function
    ) -> None:
        """
        检查SLA违规并触发告警
        
        Args:
            latency_ms: 测量的延迟（毫秒）
            function: 函数定义（包含timeout/SLA目标）
        """
        # 获取SLA目标（假设函数有deadline或timeout）
        sla_target_ms = function.timeout * 1000 * 0.8  # 使用timeout的80%作为SLA

        # 维护延迟滑动窗口
        if function_id not in self.latency_windows:
            self.latency_windows[function_id] = deque(maxlen=self.window_size)

        window = self.latency_windows[function_id]
        window.append(latency_ms)

        # 计算百分位数（使用窗口内的数据）
        if len(window) >= 3:
            p95_latency = np.percentile(list(window), 95)

            # 检查是否违规
            violation_factor = p95_latency / sla_target_ms if sla_target_ms > 0 else 1.0

            if violation_factor > self.alert_threshold:
                alert = Alert(
                    timestamp=time.time(),
                    machine_id=machine_id,
                    function_id=function_id,
                    invocation_id="N/A",  # 需要从invocation追踪
                    measured_latency_ms=p95_latency,
                    sla_target_ms=sla_target_ms,
                    violation_factor=violation_factor,
                    severity="critical" if violation_factor > 2.0 else "warning"
                )

                print(
                    f"[Monitor] ⚠️ SLA违规: {function.name} "
                    f"延迟={p95_latency:.1f}ms (目标={sla_target_ms:.1f}ms) "
                    f"超标={violation_factor:.2f}x"
                )

                # 触发回调
                if self.callback:
                    self.callback(alert)

    def _update_contention_estimate(
        self,
        machine_id: str,
        sample: MonitoringSample
    ) -> None:
        """
        更新机器的CPU争抢程度估计
        
        方法：基于cgroup的CPU使用率和shares分配计算
        """
        machine = self.machines[machine_id]

        # 计算总的shares分配
        total_shares = machine.total_cpu_shares_allocated
        physical_shares = machine.cpu_cores * 100  # v2 weight

        if total_shares <= physical_shares:
            contention = 0.0
        else:
            # 争抢比例 = (超卖量) / (总分配量)
            contention = (total_shares - physical_shares) / total_shares * 100

        # 结合实际CPU使用率进行校正
        cpu_usage = sample.cpu_usage
        # 如果使用率低于分配，说明争抢不严重
        if cpu_usage < 0.8:
            contention *= cpu_usage / 0.8

        self.contention_levels[machine_id] = contention

    def get_contention_level(self, machine_id: str) -> float:
        """获取机器的当前CPU争抢程度"""
        return self.contention_levels.get(machine_id, 0.0)

    def get_function_latency_stats(
        self,
        function_id: str
    ) -> Optional[Dict[str, float]]:
        """获取函数的延迟统计"""
        window = self.latency_windows.get(function_id)
        if not window or len(window) < 1:
            return None

        data = list(window)
        return {
            "mean": np.mean(data),
            "p50": np.percentile(data, 50),
            "p95": np.percentile(data, 95),
            "p99": np.percentile(data, 99),
            "max": np.max(data)
        }

    def get_machine_stats(self, machine_id: str) -> dict:
        """获取机器的监控统计"""
        samples = self.samples.get(machine_id, [])
        if not samples:
            return {}

        cpu_usages = [s.cpu_usage for s in samples]
        return {
            "cpu_usage_avg": np.mean(cpu_usages),
            "cpu_usage_max": np.max(cpu_usages),
            "sample_count": len(samples),
            "contention_level": self.contention_levels.get(machine_id, 0.0)
        }


async def main():
    """测试监控器"""
    from types import SimpleNamespace

    # 创建测试机器
    machines = {
        "machine_0": Machine(
            machine_id="machine_0",
            cpu_cores=8,
            memory_mb=32*1024
        )
    }

    monitor = Monitor(machines, sample_interval=0.5)

    try:
        await monitor.start()
        await asyncio.sleep(5)  # 运行5秒
    finally:
        await monitor.stop()

    # 输出统计
    stats = monitor.get_machine_stats("machine_0")
    print(f"机器统计: {stats}")


if __name__ == "__main__":
    asyncio.run(main())

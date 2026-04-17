"""
FaaS仿真框架 - Simulator

完整的FaaS环境仿真，支持：
1. 模拟多台机器的FaaS集群
2. 生成符合真实分布的函数调用请求
3. 注入CPU干扰事件（周期、突发、持续）
4. 运行调度、监控、调节全流程
5. 收集性能指标和实验结果
"""

import asyncio
import time
import random
import yaml
import json
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field
from collections import defaultdict, deque
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

from .types import (
    Function, Invocation, Machine, FunctionState,
    SensitivityProfile, ExperimentResult
)
from .scheduler import ResilientScheduler
from .monitor import Monitor
from .regulator import AdaptiveRegulator
from .sensitivity_profiler import SensitivityProfiler
from .workload_generator import WorkloadGenerator


@dataclass
class SimulationConfig:
    """仿真配置"""
    duration: float = 3600.0       # 仿真时长（秒）
    warmup: float = 60.0           # 预热时间
    num_machines: int = 4           # 机器数量
    cpu_cores_per_machine: int = 8 # 每机器CPU核心数
    memory_per_machine_gb: int = 32
    scheduler_strategy: str = "resilient"
    interference_enabled: bool = True
    interference_pattern: str = "mixed"  # "periodic" | "burst" | "sustained" | "mixed"


@dataclass
class SimulationEvent:
    """仿真事件"""
    timestamp: float
    event_type: str  # "invocation" | "interference_start" | "interference_end" | "completion"
    data: dict


class ResilientCPUSimulator:
    """
    ResilientCPU 仿真器

    完整模拟FaaS环境，用于评估调度策略和敏感度曲线机制的效果
    """

    def __init__(self, config: SimulationConfig):
        self.config = config
        self.sim_time = 0.0

        # 初始化组件
        self.machines: Dict[str, Machine] = {}
        self.functions: Dict[str, Function] = {}
        self.invocation_queue: asyncio.Queue = asyncio.Queue()
        self.active_invocations: Dict[str, Invocation] = {}
        self.completed_invocations: List[Invocation] = []

        # 调度器和监控
        self.scheduler: Optional[ResilientScheduler] = None
        self.monitor: Optional[Monitor] = None
        self.regulator: Optional[AdaptiveRegulator] = None
        self.profiler: SensitivityProfiler = SensitivityProfiler()

        # 工作负载生成器
        self.workload_gen: Optional[WorkloadGenerator] = None

        # 事件记录
        self.events: List[SimulationEvent] = []
        self.invocation_latencies: List[float] = []
        self.sla_violations: List[Invocation] = []

        # 干扰注入器
        self.interference_events: List[dict] = []

        # 运行状态
        self.running = False
        self.tasks: List[asyncio.Task] = []

        # 初始化
        self._initialize()

    def _initialize(self) -> None:
        """初始化仿真环境"""
        print(f"[Simulator] 初始化仿真环境...")

        # 创建机器
        for i in range(self.config.num_machines):
            machine = Machine(
                machine_id=f"machine_{i}",
                cpu_cores=self.config.cpu_cores_per_machine,
                memory_mb=self.config.memory_per_machine_gb * 1024,
                max_cpu_shares=self.config.cpu_cores_per_machine * 1024
            )
            self.machines[machine.machine_id] = machine

        # 创建默认函数集
        self._create_default_functions()

        # 初始化调度器
        self.scheduler = ResilientScheduler(
            machines=list(self.machines.values()),
            strategy=self.config.scheduler_strategy
        )

        # 注册所有函数
        for func in self.functions.values():
            self.scheduler.register_function(func)

        # 初始化监控器
        self.monitor = Monitor(
            machines=self.machines,
            sample_interval=0.1
        )
        self.scheduler.set_monitor(self.monitor)

        # 初始化调节器
        self.regulator = AdaptiveRegulator(
            machines=self.machines,
            scheduler=self.scheduler,
            monitor=self.monitor
        )

        print(f"[Simulator] 初始化完成: {len(self.machines)}台机器, {len(self.functions)}个函数")

    def _create_default_functions(self) -> None:
        """创建默认函数集（模拟真实FaaS场景）"""

        function_specs = [
            # 名称, CPU需求, 内存, 基准延迟(s), 敏感度类型
            ("ImageResize", 1.0, 512, 0.5, "high"),      # 图片处理，高敏感
            ("ImageFilter", 2.0, 1024, 1.2, "high"),
            ("DataCompress", 1.5, 256, 2.0, "medium"),
            ("TextParse", 0.5, 256, 0.3, "low"),
            ("MLInference", 3.0, 2048, 0.8, "medium"),
            ("LogProcess", 0.5, 512, 0.1, "low"),
            ("DBCheck", 1.0, 1024, 0.5, "medium"),
            ("APIGateway", 0.5, 256, 0.05, "low"),
            ("MediaTranscode", 4.0, 2048, 3.0, "high"),
            ("ReportGen", 2.0, 1024, 1.5, "medium"),
        ]

        sensitivity_curves = {
            "high": [(0, 0), (20, 0.08), (40, 0.25), (60, 0.55), (80, 0.85), (100, 0.98)],
            "medium": [(0, 0), (20, 0.03), (40, 0.10), (60, 0.25), (80, 0.50), (100, 0.80)],
            "low": [(0, 0), (20, 0.01), (40, 0.03), (60, 0.08), (80, 0.18), (100, 0.40)]
        }

        for name, cpu, mem, latency, sensitivity in function_specs:
            func_id = name.lower().replace(" ", "_")
            curve = sensitivity_curves[sensitivity]

            # 创建敏感度曲线
            profile = SensitivityProfile(
                function_id=func_id,
                sensitivity_points=curve,
                knee_point=self._estimate_knee(curve),
                acceptable_degradation=0.05
            )

            func = Function(
                function_id=func_id,
                name=name,
                memory_mb=mem,
                cpu_cores=cpu,
                execution_time_mean=latency,
                execution_time_std=latency * 0.2,
                timeout=latency * 3,
                sensitivity_profile=profile
            )
            self.functions[func_id] = func

    def _estimate_knee(self, curve: List[tuple]) -> float:
        """从敏感度曲线估计拐点"""
        # 找到性能退化开始加速的点
        for i in range(1, len(curve) - 1):
            x1, y1 = curve[i-1]
            x2, y2 = curve[i]
            x3, y3 = curve[i+1]

            # 计算曲率（简化的二阶差分）
            if x2 != x1 and x3 != x2:
                slope1 = (y2 - y1) / (x2 - x1)
                slope2 = (y3 - y2) / (x3 - x2)
                curvature = abs(slope2 - slope1)

                if curvature > 0.01:  # 曲率阈值
                    return x2
        return 50.0

    async def run(self) -> ExperimentResult:
        """
        运行仿真

        Returns:
            ExperimentResult: 实验结果
        """
        print(f"[Simulator] 开始仿真: duration={self.config.duration}s")
        self.running = True

        start_time = time.time()

        # 生成工作负载
        self.workload_gen = WorkloadGenerator(
            functions=list(self.functions.values()),
            arrival_rate=50.0,  # 每秒50个请求
            duration=self.config.duration
        )

        # 启动组件
        await self.monitor.start()
        await self.regulator.start()

        # 创建任务
        self.tasks = [
            asyncio.create_task(self._invocation_generator()),
            asyncio.create_task(self._invocation_processor()),
            asyncio.create_task(self._interference_injector()),
            asyncio.create_task(self._metrics_collector())
        ]

        # 运行仿真
        try:
            await asyncio.gather(*self.tasks)
        except asyncio.CancelledError:
            print("[Simulator] 仿真被取消")
        finally:
            self.running = False

            # 停止组件
            await self.regulator.stop()
            await self.monitor.stop()

        elapsed = time.time() - start_time
        print(f"[Simulator] 仿真完成，耗时: {elapsed:.2f}s")

        # 生成结果
        result = self._generate_result()
        return result

    async def _invocation_generator(self) -> None:
        """生成函数调用请求"""
        for invocation in self.workload_gen.generate():
            if not self.running:
                break

            # 等待到达时间
            await asyncio.sleep(invocation.duration)  # duration在这里用作"到达间隔"

            if self.running:
                self.active_invocations[invocation.invocation_id] = invocation
                await self.invocation_queue.put(invocation)

                self.events.append(SimulationEvent(
                    timestamp=time.time(),
                    event_type="invocation",
                    data={"invocation_id": invocation.invocation_id}
                ))

    async def _invocation_processor(self) -> None:
        """处理函数调用"""
        while self.running:
            try:
                invocation = await asyncio.wait_for(
                    self.invocation_queue.get(),
                    timeout=1.0
                )

                # 调度
                decision = await self.scheduler.schedule(invocation)
                invocation.start_time = time.time()

                if decision:
                    # 模拟执行
                    asyncio.create_task(self._execute_function(invocation, decision))
                else:
                    # 调度失败
                    invocation.sla_violated = True
                    self.sla_violations.append(invocation)

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[Simulator] 处理请求错误: {e}")

    async def _execute_function(
        self,
        invocation: Invocation,
        decision
    ) -> None:
        """执行函数"""
        func = self.functions[invocation.function_id]
        machine = self.machines[decision.selected_machine]

        # 模拟函数执行
        # 考虑敏感度曲线：争抢越严重，执行时间越长
        contention = machine.cpu_contention
        if func.sensitivity_profile:
            degradation = func.sensitivity_profile.get_performance_degradation(contention)
        else:
            degradation = contention / 100 * 0.5  # 默认敏感度

        # 计算实际执行时间
        execution_time = func.execution_time_mean * (1 + degradation * random.uniform(0.5, 1.5))
        execution_time = max(0.01, execution_time)

        await asyncio.sleep(execution_time)

        # 完成
        invocation.end_time = time.time()
        invocation.duration = invocation.end_time - invocation.start_time
        invocation.sla_violated = invocation.duration > func.timeout

        # 记录
        self.invocation_latencies.append(invocation.duration * 1000)  # 转换为ms

        if invocation.sla_violated:
            self.sla_violations.append(invocation)

        # 更新机器状态
        await self.scheduler.restore_resources(invocation.function_id, decision.selected_machine)

        # 清理
        if invocation.invocation_id in self.active_invocations:
            del self.active_invocations[invocation.invocation_id]
        self.completed_invocations.append(invocation)

        self.events.append(SimulationEvent(
            timestamp=time.time(),
            event_type="completion",
            data={"invocation_id": invocation.invocation_id, "duration": invocation.duration}
        ))

    async def _interference_injector(self) -> None:
        """注入CPU干扰"""
        if not self.config.interference_enabled:
            return

        pattern = self.config.interference_pattern

        while self.running:
            # 随机选择干扰模式
            if pattern == "mixed":
                pattern = random.choice(["periodic", "burst", "sustained"])

            if pattern == "periodic":
                # 周期性干扰：每30秒持续10秒
                await asyncio.sleep(30.0)
                if self.running:
                    await self._inject_burst_interference(duration=10.0, intensity=0.3)

            elif pattern == "burst":
                # 突发干扰：持续3-8秒
                await asyncio.sleep(random.uniform(5.0, 20.0))
                if self.running:
                    intensity = random.uniform(0.2, 0.5)
                    duration = random.uniform(3.0, 8.0)
                    await self._inject_burst_interference(duration=duration, intensity=intensity)

            elif pattern == "sustained":
                # 持续干扰：低强度长时间
                await asyncio.sleep(10.0)
                if self.running:
                    await self._inject_sustained_interference(duration=60.0, intensity=0.15)

            else:
                await asyncio.sleep(10.0)

    async def _inject_burst_interference(self, duration: float, intensity: float) -> None:
        """注入突发干扰"""
        print(f"[Simulator] 注入突发干扰: duration={duration:.1f}s, intensity={intensity:.2f}")

        self.events.append(SimulationEvent(
            timestamp=time.time(),
            event_type="interference_start",
            data={"pattern": "burst", "intensity": intensity}
        ))

        # 增加所有机器的CPU争抢
        for machine in self.machines.values():
            original_contention = machine.cpu_contention
            machine.cpu_contention = min(100.0, original_contention + intensity * 100)
            machine.cpu_utilization = min(1.0, machine.cpu_utilization + intensity)

        # 持续干扰时间
        await asyncio.sleep(duration)

        # 恢复
        for machine in self.machines.values():
            machine.cpu_contention = max(0.0, machine.cpu_contention - intensity * 100)
            machine.cpu_utilization = max(0.0, machine.cpu_utilization - intensity)

        self.events.append(SimulationEvent(
            timestamp=time.time(),
            event_type="interference_end",
            data={"pattern": "burst"}
        ))

    async def _inject_sustained_interference(self, duration: float, intensity: float) -> None:
        """注入持续干扰"""
        print(f"[Simulator] 注入持续干扰: duration={duration:.1f}s, intensity={intensity:.2f}")

        for machine in self.machines.values():
            machine.cpu_contention += intensity * 50
            machine.cpu_contention = min(100.0, machine.cpu_contention)

        await asyncio.sleep(duration)

        for machine in self.machines.values():
            machine.cpu_contention = max(0.0, machine.cpu_contention - intensity * 50)

    async def _metrics_collector(self) -> None:
        """定期收集指标"""
        while self.running:
            await asyncio.sleep(5.0)  # 每5秒

            if self.running and len(self.completed_invocations) > 0:
                # 打印进度
                total = len(self.completed_invocations) + len(self.active_invocations)
                sla_rate = len(self.sla_violations) / total * 100
                print(
                    f"[Simulator] 进度: {len(self.completed_invocations)}/{total} 完成, "
                    f"SLA违规率: {sla_rate:.2f}%"
                )

    def _generate_result(self) -> ExperimentResult:
        """生成实验结果"""
        latencies = self.invocation_latencies
        total = len(latencies)

        if total == 0:
            return ExperimentResult(
                experiment_id="empty",
                scheduler_type=self.config.scheduler_strategy,
                avg_latency=0, p95_latency=0, p99_latency=0,
                sla_violation_rate=0, throughput=0,
                avg_cpu_utilization=0, peak_cpu_utilization=0,
                resource_efficiency=0, latency_cv=0, profiling_error=0
            )

        latencies_arr = np.array(latencies)

        # 计算指标
        avg_latency = np.mean(latencies_arr)
        p95_latency = np.percentile(latencies_arr, 95)
        p99_latency = np.percentile(latencies_arr, 99)
        sla_violation_rate = len(self.sla_violations) / total
        throughput = total / self.config.duration

        # CPU利用率
        utilizations = [m.cpu_utilization for m in self.machines.values()]
        avg_cpu = np.mean(utilizations)
        peak_cpu = np.max(utilizations)

        # 资源效率 = 有效工作 / 消耗资源
        total_allocated_cores = sum(m.total_allocated_cores() for m in self.machines.values())
        resource_efficiency = (total - len(self.sla_violations)) / total if total > 0 else 0

        # 延迟变异系数
        latency_cv = np.std(latencies_arr) / avg_latency if avg_latency > 0 else 0

        return ExperimentResult(
            experiment_id=f"{self.config.scheduler_strategy}_{int(time.time())}",
            scheduler_type=self.config.scheduler_strategy,
            avg_latency=avg_latency,
            p95_latency=p95_latency,
            p99_latency=p99_latency,
            sla_violation_rate=sla_violation_rate,
            throughput=throughput,
            avg_cpu_utilization=avg_cpu,
            peak_cpu_utilization=peak_cpu,
            resource_efficiency=resource_efficiency,
            latency_cv=latency_cv,
            profiling_error=0.05  # 简化
        )

    def plot_results(self, output_dir: str = "results") -> None:
        """绘制结果图表"""
        import os
        os.makedirs(output_dir, exist_ok=True)

        latencies = self.invocation_latencies
        if not latencies:
            return

        # 1. 延迟分布直方图
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))

        axes[0, 0].hist(latencies, bins=50, edgecolor='black', alpha=0.7)
        axes[0, 0].set_xlabel('延迟 (ms)')
        axes[0, 0].set_ylabel('频次')
        axes[0, 0].set_title('延迟分布')
        axes[0, 0].axvline(np.percentile(latencies, 95), color='r', linestyle='--',
                          label=f'P95: {np.percentile(latencies, 95):.1f}ms')
        axes[0, 0].legend()

        # 2. CDF曲线
        sorted_latencies = np.sort(latencies)
        cdf = np.arange(1, len(sorted_latencies) + 1) / len(sorted_latencies)
        axes[0, 1].plot(sorted_latencies, cdf)
        axes[0, 1].set_xlabel('延迟 (ms)')
        axes[0, 1].set_ylabel('累积概率')
        axes[0, 1].set_title('延迟累积分布函数 (CDF)')
        axes[0, 1].grid(True, alpha=0.3)

        # 3. SLA违规率
        sla_rates = []
        for threshold in [1.0, 1.5, 2.0, 3.0]:
            rate = sum(1 for l in latencies if l > threshold * 1000) / len(latencies)
            sla_rates.append(rate)

        axes[1, 0].bar(['1x', '1.5x', '2x', '3x'], sla_rates, color='salmon')
        axes[1, 0].set_xlabel('SLA倍数')
        axes[1, 0].set_ylabel('违规率')
        axes[1, 0].set_title('不同SLA阈值下的违规率')
        axes[1, 0].set_ylim(0, 0.5)

        # 4. 机器利用率
        machine_ids = list(self.machines.keys())
        utilizations = [m.cpu_utilization for m in self.machines.values()]
        axes[1, 1].bar(machine_ids, utilizations, color='steelblue')
        axes[1, 1].set_xlabel('机器ID')
        axes[1, 1].set_ylabel('CPU利用率')
        axes[1, 1].set_title('机器CPU利用率')
        axes[1, 1].set_ylim(0, 1.0)

        plt.tight_layout()
        plt.savefig(f'{output_dir}/simulation_results.png', dpi=150)
        plt.close()

        print(f"[Simulator] 结果图表已保存到 {output_dir}/simulation_results.png")

    def save_results(self, output_dir: str = "results") -> None:
        """保存结果到文件"""
        import os
        os.makedirs(output_dir, exist_ok=True)

        result = self._generate_result()

        with open(f'{output_dir}/experiment_result.json', 'w') as f:
            json.dump({
                "experiment_id": result.experiment_id,
                "scheduler_type": result.scheduler_type,
                "metrics": {
                    "avg_latency_ms": result.avg_latency,
                    "p95_latency_ms": result.p95_latency,
                    "p99_latency_ms": result.p99_latency,
                    "sla_violation_rate": result.sla_violation_rate,
                    "throughput_per_sec": result.throughput,
                    "avg_cpu_utilization": result.avg_cpu_utilization,
                    "resource_efficiency": result.resource_efficiency,
                    "latency_cv": result.latency_cv
                },
                "config": {
                    "duration": self.config.duration,
                    "num_machines": self.config.num_machines,
                    "interference_enabled": self.config.interference_enabled,
                    "interference_pattern": self.config.interference_pattern
                },
                "total_invocations": len(self.completed_invocations),
                "total_violations": len(self.sla_violations)
            }, f, indent=2)

        print(f"[Simulator] 结果已保存到 {output_dir}/experiment_result.json")


async def main():
    """运行默认仿真"""
    config = SimulationConfig(
        duration=60.0,      # 60秒仿真
        warmup=10.0,
        num_machines=4,
        cpu_cores_per_machine=8,
        scheduler_strategy="resilient",
        interference_enabled=True,
        interference_pattern="burst"
    )

    simulator = ResilientCPUSimulator(config)
    result = await simulator.run()

    # 输出结果
    print("\n" + "="*50)
    print("仿真结果:")
    print(f"  平均延迟: {result.avg_latency:.2f} ms")
    print(f"  P95延迟: {result.p95_latency:.2f} ms")
    print(f"  P99延迟: {result.p99_latency:.2f} ms")
    print(f"  SLA违规率: {result.sla_violation_rate*100:.2f}%")
    print(f"  吞吐量: {result.throughput:.2f} req/s")
    print(f"  CPU利用率: {result.avg_cpu_utilization*100:.1f}%")
    print("="*50)

    # 保存和绘图
    simulator.save_results()
    simulator.plot_results()


if __name__ == "__main__":
    asyncio.run(main())

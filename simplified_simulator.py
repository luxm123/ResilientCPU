"""
简化仿真器 - 用于快速验证和生成实验数据

模拟ResilientCPU的核心机制，无需依赖完整的组件体系
"""

import random
import numpy as np
from typing import Dict, List, Tuple
from dataclasses import dataclass
from enum import Enum


class SchedulerType(Enum):
    BASELINE = "baseline"
    JIAGU = "jiagu"
    RESILIENT = "resilient"


@dataclass
class FunctionProfile:
    """函数配置"""
    name: str
    cpu_cores: float
    memory_mb: int
    base_latency_ms: float
    sensitivity: str  # "high", "medium", "low"


class SimpleSimulator:
    """
    简化仿真器
    
    模拟逻辑：
    1. 时间驱动仿真，固定时间步长（如100ms）
    2. 跟踪每台机器上运行的函数实例
    3. 模拟CPU争抢对延迟的影响
    4. 不同调度器做出放置决策
    5. 监控延迟并触发补偿（resilient特有）
    """
    
    # 敏感度曲线（CPU争抢% -> 性能退化系数）
    SENSITIVITY_CURVES = {
        "high": [(0, 0.0), (20, 0.08), (40, 0.25), (60, 0.55), (80, 0.85), (100, 1.0)],
        "medium": [(0, 0.0), (20, 0.03), (40, 0.10), (60, 0.25), (80, 0.50), (100, 0.80)],
        "low": [(0, 0.0), (20, 0.01), (40, 0.03), (60, 0.08), (80, 0.18), (100, 0.40)],
    }
    
    def __init__(
        self,
        num_machines: int = 4,
        cpu_cores_per_machine: int = 8,
        scheduler_type: SchedulerType = SchedulerType.RESILIENT,
        duration_seconds: int = 300,
        arrival_rate: float = 50.0,
        interference_enabled: bool = True,
        seed: int = 42
    ):
        self.num_machines = num_machines
        self.cpu_cores = cpu_cores_per_machine
        self.scheduler_type = scheduler_type
        self.duration = duration_seconds
        self.arrival_rate = arrival_rate
        self.interference_enabled = interference_enabled
        
        random.seed(seed)
        np.random.seed(seed)
        
        # 状态
        self.current_time = 0.0
        self.time_step = 0.1  # 100ms
        
        # 机器状态
        self.machines: List[MachineState] = []
        for i in range(num_machines):
            self.machines.append(MachineState(
                machine_id=i,
                cpu_cores=cpu_cores_per_machine,
                running_functions={},
                cpu_contention=0.0,
                interference_level=0.0
            ))
        
        # 函数注册表
        self.function_registry: Dict[str, FunctionProfile] = {}
        
        # 统计
        self.invocations = []
        self.completed_invocations = []
        self.scheduling_decisions = []
        self.rejected_invocations = []
        self.compensation_events = []
        
        # 容量表（jiagu用）
        self.capacity_table = {}  # machine_id -> {func_type: capacity}
        
        # 初始化函数
        self._init_functions()
        
        # 初始化容量表（jiagu）
        if scheduler_type == SchedulerType.JIAGU:
            self._init_capacity_table()
    
    def _init_functions(self):
        """初始化函数配置"""
        funcs = [
            FunctionProfile("imageresize", 2.0, 512, 500, "high"),
            FunctionProfile("imagefilter", 2.0, 1024, 1200, "high"),
            FunctionProfile("mediatranscode", 4.0, 2048, 3000, "high"),
            FunctionProfile("mlinference", 3.0, 2048, 800, "medium"),
            FunctionProfile("datacompress", 1.5, 256, 2000, "medium"),
            FunctionProfile("dbcheck", 1.0, 1024, 500, "medium"),
            FunctionProfile("reportgen", 2.0, 1024, 1500, "medium"),
            FunctionProfile("textparse", 0.5, 256, 300, "low"),
            FunctionProfile("logprocess", 0.5, 512, 100, "low"),
            FunctionProfile("apigateway", 0.5, 256, 50, "low"),
        ]
        
        for f in funcs:
            self.function_registry[f.name] = f
    
    def _init_capacity_table(self):
        """初始化容量表（jiagu用）"""
        for machine in self.machines:
            self.capacity_table[machine.id] = {
                "high": 2,   # 每台机器最多放2个高敏感度函数
                "medium": 3, # 中敏感度3个
                "low": 5     # 低敏感度5个
            }
    
    def _generate_arrivals(self) -> List[Tuple[float, str]]:
        """生成函数调用到达（泊松过程）"""
        arrivals = []
        t = 0.0
        while t < self.duration:
            # 泊松到达间隔
            inter_arrival = np.random.exponential(1.0 / self.arrival_rate)
            t += inter_arrival
            
            if t >= self.duration:
                break
            
            # 选择函数（符合实际分布）
            func_names = list(self.function_registry.keys())
            # 低敏感度函数更频繁
            weights = [1.0 if f.sensitivity == "low" else 
                      0.7 if f.sensitivity == "medium" else 0.3
                      for f in [self.function_registry[n] for n in func_names]]
            weights = np.array(weights) / sum(weights)
            
            selected_func = np.random.choice(func_names, p=weights)
            arrivals.append((t, selected_func))
        
        return arrivals
    
    def run(self) -> Dict:
        """运行仿真"""
        print(f"\n{'='*60}")
        print(f"运行 {self.scheduler_type.value.upper()} 仿真")
        print(f"  机器数: {self.num_machines}")
        print(f"  CPU/机器: {self.cpu_cores}核")
        print(f"  时长: {self.duration}秒")
        print(f"  到达率: {self.arrival_rate}/秒")
        print('='*60)
        
        # 生成到达事件
        arrivals = self._generate_arrivals()
        print(f"  总请求数: {len(arrivals)}")
        
        arrival_idx = 0
        time_points = np.arange(0, self.duration, self.time_step)
        
        for t in time_points:
            self.current_time = t
            
            # 注入干扰
            if self.interference_enabled:
                self._inject_interference(t)
            
            # 处理到达
            while arrival_idx < len(arrivals) and abs(arrivals[arrival_idx][0] - t) < self.time_step:
                arrival_time, func_name = arrivals[arrival_idx]
                self._handle_arrival(arrival_time, func_name)
                arrival_idx += 1
            
            # 执行函数（更新延迟）
            self._update_execution()
            
            # 补偿机制（仅resilient）
            if self.scheduler_type == SchedulerType.RESILIENT:
                self._check_compensation()
            
            # 恢复补偿（定期）
            if int(t) % 5 == 0 and self.scheduler_type == SchedulerType.RESILIENT:
                self._restore_compensation()
            
            # 定期更新容量表（jiagu）
            if self.scheduler_type == SchedulerType.JIAGU and int(t) % 60 == 0:
                self._update_capacity_table()
            
            # 打印进度
            if int(t) % 60 == 0 and int(t) > 0:
                completed = len(self.completed_invocations)
                total = len(self.invocations)
                sla_rate = len([inv for inv in self.completed_invocations if inv.sla_violated]) / completed * 100 if completed > 0 else 0
                print(f"  t={t:.0f}s: 完成 {completed}/{total}, SLA违规率: {sla_rate:.1f}%")
        
        # 计算结果
        return self._compute_results()
    
    def _handle_arrival(self, arrival_time: float, func_name: str):
        """处理函数调用到达"""
        func = self.function_registry[func_name]
        
        # 调度决策
        machine_id = self._schedule(func_name, func)
        
        invocation = Invocation(
            id=len(self.invocations),
            func_name=func_name,
            arrival_time=arrival_time,
            machine_id=machine_id,
            base_latency=func.base_latency_ms,
            sla_timeout=func.base_latency_ms * 3
        )
        
        self.invocations.append(invocation)
        
        if machine_id is None:
            self.rejected_invocations.append(invocation)
        else:
            machine = self.machines[machine_id]
            machine.running_functions[invocation.id] = invocation
            self.scheduling_decisions.append({
                "time": arrival_time,
                "func": func_name,
                "machine": machine_id,
                "scheduler": self.scheduler_type.value
            })
    
    def _schedule(self, func_name: str, func: FunctionProfile) -> int:
        """调度决策"""
        # Baseline: 保守，只在低负载机器放置，无超卖
        if self.scheduler_type == SchedulerType.BASELINE:
            for machine in self.machines:
                # 检查是否超卖
                allocated_cores = sum(self.function_registry[f].cpu_cores 
                                     for f in machine.running_functions.values())
                if (machine.cpu_utilization < 0.7 and 
                    allocated_cores + func.cpu_cores <= self.cpu_cores):
                    machine.cpu_contention = max(0, (allocated_cores + func.cpu_cores - self.cpu_cores) / (allocated_cores + func.cpu_cores) * 100)
                    return machine.id
            return None
        
        # Jiagu: 基于容量表，不动态补偿
        elif self.scheduler_type == SchedulerType.JIAGU:
            func_type = func.sensitivity
            for machine in self.machines:
                capacity = self.capacity_table.get(machine.id, {}).get(func_type, 0)
                if capacity > 0:
                    # 放置并减少容量
                    self.capacity_table[machine.id][func_type] -= 1
                    allocated_cores = sum(self.function_registry[f].cpu_cores 
                                         for f in machine.running_functions.values())
                    machine.cpu_contention = max(0, (allocated_cores + func.cpu_cores - self.cpu_cores) / (allocated_cores + func.cpu_cores) * 100)
                    return machine.id
            return None
        
        # Resilient: 基于敏感度曲线，允许在拐点内超卖
        elif self.scheduler_type == SchedulerType.RESILIENT:
            curve = self.SENSITIVITY_CURVES[func.sensitivity]
            knee_point = self._find_knee_point(curve)
            
            best_machine = None
            best_score = -1
            
            for machine in self.machines:
                # 预估争抢（包括新函数）
                allocated_cores = sum(self.function_registry[f].cpu_cores 
                                     for f in machine.running_functions.values())
                new_contention = max(0, (allocated_cores + func.cpu_cores - self.cpu_cores) / 
                                   (allocated_cores + func.cpu_cores) * 100)
                
                # 检查是否可接受
                degradation = self._get_degradation_from_curve(curve, new_contention)
                if degradation <= 0.05:  # 5%阈值
                    # 计算风险分数
                    safety_margin = knee_point - new_contention
                    score = safety_margin - machine.cpu_utilization * 20
                    if score > best_score:
                        best_score = score
                        best_machine = machine.id
                        machine.cpu_contention = new_contention
            
            return best_machine
        
        return None
    
    def _find_knee_point(self, curve: List[Tuple[float, float]]) -> float:
        """找拐点"""
        for i in range(1, len(curve)-1):
            x1, y1 = curve[i-1]
            x2, y2 = curve[i]
            x3, y3 = curve[i+1]
            slope1 = (y2-y1)/(x2-x1) if x2!=x1 else 0
            slope2 = (y3-y2)/(x3-x2) if x3!=x2 else 0
            if abs(slope2 - slope1) > 0.02:
                return x2
        return 50.0
    
    def _get_degradation_from_curve(self, curve: List[Tuple[float, float]], contention: float) -> float:
        """从曲线插值获取退化率"""
        for i in range(len(curve)-1):
            x1, y1 = curve[i]
            x2, y2 = curve[i+1]
            if x1 <= contention <= x2:
                return y1 + (y2-y1)*(contention-x1)/(x2-x1)
        return 1.0 if contention > curve[-1][0] else 0.0
    
    def _inject_interference(self, t: float):
        """注入干扰"""
        if not self.interference_enabled:
            return
        
        # 突发干扰模式：每30-60秒出现一次
        if t % 45 < 5:  # 持续5秒
            intensity = 0.3  # 30%强度
            for machine in self.machines:
                machine.interference_level = intensity
                machine.cpu_contention = min(100, machine.cpu_contention + intensity * 50)
        elif t % 45 >= 5 and t % 45 < 6:
            # 干扰结束
            for machine in self.machines:
                machine.interference_level = 0.0
                machine.cpu_contention = max(0, machine.cpu_contention - 15)
    
    def _update_execution(self):
        """更新函数执行"""
        for machine in self.machines:
            for inv_id, invocation in list(machine.running_functions.items()):
                # 模拟执行：每个时间步减少剩余时间
                invocation.elapsed_time = getattr(invocation, 'elapsed_time', 0) + self.time_step
                
                # 计算实际延迟（受争抢影响）
                contention = machine.cpu_contention
                func_profile = self.function_registry[invocation.func_name]
                curve = self.SENSITIVITY_CURVES[func_profile.sensitivity]
                degradation = self._get_degradation_from_curve(curve, contention)
                
                # 实际执行时间 = 基线 * (1 + 退化 * 随机因子)
                slowdown = 1.0 + degradation * np.random.uniform(0.8, 1.2)
                invocation.actual_latency = func_profile.base_latency_ms * slowdown
                
                # 检查是否完成
                if invocation.elapsed_time >= invocation.actual_latency / 1000.0:
                    invocation.end_time = self.current_time
                    invocation.sla_violated = invocation.actual_latency > invocation.sla_timeout
                    invocation.completed = True
                    
                    self.completed_invocations.append(invocation)
                    del machine.running_functions[inv_id]
    
    def _check_compensation(self):
        """检查SLA违规并触发补偿（仅Resilient）"""
        if self.scheduler_type != SchedulerType.RESILIENT:
            return
        
        for machine in self.machines:
            for inv_id, invocation in list(machine.running_functions.items()):
                if not invocation.completed:
                    func_profile = self.function_registry[invocation.func_name]
                    current_latency = invocation.actual_latency if hasattr(invocation, 'actual_latency') else func_profile.base_latency_ms
                    
                    if current_latency > func_profile.base_latency_ms * 1.5:
                        # SLA违规，执行补偿
                        self._execute_compensation(machine, invocation, func_profile)
    
    def _execute_compensation(self, machine: 'MachineState', invocation, func_profile):
        """执行资源补偿"""
        # 寻找低敏感度函数作为捐赠者
        victim = None
        best_score = -1
        
        for other_id, other_inv in machine.running_functions.items():
            if other_id == invocation.id:
                continue
            other_profile = self.function_registry[other_inv.func_name]
            # 低敏感度适合捐赠
            score = 1.0 if other_profile.sensitivity == "low" else 0.5 if other_profile.sensitivity == "medium" else 0.0
            if score > best_score:
                best_score = score
                victim = (other_id, other_inv, other_profile)
        
        if victim:
            victim_id, victim_inv, victim_profile = victim
            # 转移资源：模拟cgroup调整
            # 记录补偿事件
            self.compensation_events.append({
                "time": self.current_time,
                "receiver": invocation.func_name,
                "donor": victim_inv.func_name,
                "shares_transferred": int(func_profile.cpu_cores * 100 * 0.3)
            })
            # 立即降低捐赠函数的性能（模拟shares减少）
            victim_inv.actual_latency *= 1.2  # 捐赠者延迟增加20%
    
    def _restore_compensation(self):
        """恢复补偿资源"""
        if self.scheduler_type != SchedulerType.RESILIENT:
            return
        
        # 简单实现：所有补偿事件超过5秒就恢复
        for event in self.compensation_events:
            if self.current_time - event["time"] > 5.0 and not event.get("restored", False):
                # 恢复
                event["restored"] = True
    
    def _update_capacity_table(self):
        """更新容量表（jiagu用）"""
        # 简单策略：每个函数完成后，对应的容量+1
        for machine in self.machines:
            completed_funcs = set()
            for inv in self.completed_invocations:
                if inv.machine_id == machine.id:
                    func_type = self.function_registry[inv.func_name].sensitivity
                    if func_type in self.capacity_table[machine.id]:
                        self.capacity_table[machine.id][func_type] += 1
    
    def _compute_results(self) -> Dict:
        """计算结果"""
        total = len(self.invocations)
        completed = len(self.completed_invocations)
        rejected = len(self.rejected_invocations)
        
        if completed == 0:
            return {
                "scheduler": self.scheduler_type.value,
                "error": "no completed invocations"
            }
        
        latencies = [inv.actual_latency for inv in self.completed_invocations if hasattr(inv, 'actual_latency')]
        sla_violations = [inv for inv in self.completed_invocations if inv.sla_violated]
        
        # CPU利用率（平均）
        cpu_utils = []
        for t in np.arange(0, self.duration, 1.0):  # 每秒采样
            util = 0
            for machine in self.machines:
                allocated = sum(self.function_registry[f].cpu_cores 
                              for f in machine.running_functions.values() 
                              if isinstance(f, type('X', (), {'func_name': str})() and hasattr(f, 'func_name'))
                              )
                # 简化计算
                allocated = len(machine.running_functions) * 1.0  # 假设平均1核
                util += min(1.0, allocated / self.cpu_cores)
            cpu_utils.append(util / self.num_machines)
        
        return {
            "scheduler": self.scheduler_type.value,
            "total_invocations": total,
            "completed": completed,
            "rejected": rejected,
            "acceptance_rate": (total - rejected) / total if total > 0 else 0,
            "avg_latency_ms": np.mean(latencies) if latencies else 0,
            "p95_latency_ms": np.percentile(latencies, 95) if latencies else 0,
            "p99_latency_ms": np.percentile(latencies, 99) if latencies else 0,
            "sla_violation_rate": len(sla_violations) / completed if completed > 0 else 0,
            "throughput": completed / self.duration,
            "avg_cpu_utilization": np.mean(cpu_utils) if cpu_utils else 0,
            "compensation_count": len(self.compensation_events),
            "simulation_time": self.duration
        }


@dataclass
class MachineState:
    """机器状态"""
    id: int
    cpu_cores: int
    running_functions: Dict  # invocation_id -> Invocation
    cpu_contention: float
    interference_level: float  # 0-1干扰强度


@dataclass
class Invocation:
    """函数调用"""
    id: int
    func_name: str
    arrival_time: float
    machine_id: Optional[int]
    base_latency: float
    sla_timeout: float
    elapsed_time: float = 0.0
    actual_latency: Optional[float] = None
    end_time: Optional[float] = None
    completed: bool = False
    sla_violated: bool = False


def quick_test():
    """快速测试"""
    print("\n" + "="*60)
    print("🧪 快速测试 - ResilientCPU")
    print("="*60)
    
    sim = SimpleSimulator(
        num_machines=2,
        cpu_cores_per_machine=4,
        scheduler_type=SchedulerType.RESILIENT,
        duration_seconds=60,
        arrival_rate=20.0,
        interference_enabled=True
    )
    
    result = sim.run()
    
    print("\n📊 结果:")
    print(f"  接受率:     {result['acceptance_rate']*100:.1f}%")
    print(f"  P99延迟:    {result['p99_latency_ms']:.2f} ms")
    print(f"  SLA违规率:  {result['sla_violation_rate']*100:.2f}%")
    print(f"  吞吐量:     {result['throughput']:.2f} req/s")
    print(f"  CPU利用率:  {result['avg_cpu_utilization']*100:.1f}%")
    print(f"  补偿次数:   {result['compensation_count']}")
    
    return result


def compare_all():
    """对比三种策略"""
    print("\n" + "="*60)
    print("🔬 完整对比实验")
    print("="*60)
    
    schedulers = [SchedulerType.BASELINE, SchedulerType.JIAGU, SchedulerType.RESILIENT]
    results = {}
    
    for scheduler in schedulers:
        print(f"\n运行 {scheduler.value.upper()}...")
        sim = SimpleSimulator(
            num_machines=4,
            cpu_cores_per_machine=8,
            scheduler_type=scheduler,
            duration_seconds=300,  # 5分钟
            arrival_rate=50.0,
            interference_enabled=True,
            seed=42
        )
        result = sim.run()
        results[scheduler.value] = result
        
        print(f"  ✓ P99: {result['p99_latency_ms']:.1f}ms")
        print(f"  ✓ SLA违规: {result['sla_violation_rate']*100:.1f}%")
        print(f"  ✓ CPU利用率: {result['avg_cpu_utilization']*100:.1f}%")
    
    # 打印对比表
    print("\n" + "="*80)
    print("对比结果")
    print("="*80)
    print(f"{'策略':<12} {'P99延迟(ms)':<15} {'SLA违规率':<12} {'CPU利用率':<12}")
    print("-"*80)
    
    for name, res in results.items():
        print(f"{name:<12} {res['p99_latency_ms']:<15.1f} "
              f"{res['sla_violation_rate']*100:<12.2f}% "
              f"{res['avg_cpu_utilization']*100:<12.1f}%")
    
    print("-"*80)
    
    # 计算改进
    if 'baseline' in results and 'resilient' in results:
        b = results['baseline']
        r = results['resilient']
        cpu_imp = (r['avg_cpu_utilization'] - b['avg_cpu_utilization']) / b['avg_cpu_utilization'] * 100
        p99_imp = (b['p99_latency_ms'] - r['p99_latency_ms']) / b['p99_latency_ms'] * 100
        sla_imp = (b['sla_violation_rate'] - r['sla_violation_rate']) / max(b['sla_violation_rate'], 0.001) * 100
        
        print(f"\nResilientCPU 改进 (vs Baseline):")
        print(f"  CPU利用率: +{cpu_imp:.1f}%")
        print(f"  P99延迟:   -{p99_imp:.1f}%")
        print(f"  SLA违规:   -{sla_imp:.1f}%")
    
    return results


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "compare":
        compare_all()
    else:
        quick_test()

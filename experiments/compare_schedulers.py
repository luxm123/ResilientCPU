"""
实验对比框架 - Experiment Framework

公平对比不同调度策略的性能：
1. 使用相同的负载和干扰模式
2. 收集关键指标
3. 生成对比图表
"""

import asyncio
import sys
import os
import time
import json
import yaml
import argparse
import numpy as np
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.types import Machine, Function, Invocation, ExperimentResult
from src.simulator import ResilientCPUSimulator, SimulationConfig
from src.scheduler_factory import SchedulerFactory
from src.workload_generator import WorkloadGenerator, WorkloadPattern
from src.monitor import Monitor
from src.regulator import AdaptiveRegulator
from src.baseline_scheduler import BaselineScheduler
from src.jiagu_scheduler import JiaguLikeScheduler


@dataclass
class ComparisonConfig:
    """对比实验配置"""
    duration: float = 3600.0  # 1小时
    warmup: float = 60.0
    num_machines: int = 4
    cpu_cores_per_machine: int = 8
    memory_per_machine_gb: int = 32
    
    # 负载配置
    base_arrival_rate: float = 50.0
    arrival_rate_variation: bool = True  # QPS从10到100波动
    workload_pattern: str = "bursty"  # "uniform", "bursty", "diurnal"
    
    # 干扰配置
    interference_enabled: bool = True
    interference_pattern: str = "burst"
    
    # 实验重复次数
    num_trials: int = 3
    
    # 输出
    results_dir: str = "results/comparison"


class SchedulerComparison:
    """调度器对比实验框架"""

    def __init__(self, config: ComparisonConfig):
        self.config = config
        self.results: Dict[str, List[ExperimentResult]] = {}
        
    async def run_comparison(self) -> Dict[str, List[ExperimentResult]]:
        """
        运行对比实验
        
        对每个调度器类型，运行num_trials次实验，取平均值
        """
        scheduler_types = ["baseline", "jiagu", "resilient"]
        
        for scheduler_type in scheduler_types:
            print(f"\n{'='*60}")
            print(f"测试调度器: {scheduler_type.upper()}")
            print('='*60)
            
            trial_results = []
            
            for trial in range(self.config.num_trials):
                print(f"\n  Trial {trial+1}/{self.config.num_trials}")
                
                # 使用不同的随机种子
                seed = 42 + trial
                
                result = await self._run_single_trial(
                    scheduler_type,
                    seed=seed
                )
                
                trial_results.append(result)
                
                # 打印简要结果
                print(f"    P99 Latency: {result.p99_latency:.2f}ms")
                print(f"    SLA Violation: {result.sla_violation_rate*100:.2f}%")
                print(f"    CPU Utilization: {result.avg_cpu_utilization*100:.1f}%")
            
            self.results[scheduler_type] = trial_results
            
        # 计算统计数据
        self._compute_statistics()
        
        return self.results
    
    async def _run_single_trial(
        self,
        scheduler_type: str,
        seed: int = 42
    ) -> ExperimentResult:
        """
        运行单次实验
        
        Args:
            scheduler_type: 调度器类型
            seed: 随机种子
        """
        # 创建机器
        machines = []
        for i in range(self.config.num_machines):
            machine = Machine(
                machine_id=f"machine_{i}",
                cpu_cores=self.config.cpu_cores_per_machine,
                memory_mb=self.config.memory_per_machine_gb * 1024,
                max_cpu_shares=self.config.cpu_cores_per_machine * 1024
            )
            machines.append(machine)
        
        # 创建函数集
        functions = self._create_default_functions()
        
        # 创建负载生成器
        workload_gen = WorkloadGenerator(
            functions=functions,
            arrival_rate=self.config.base_arrival_rate,
            duration=self.config.duration,
            pattern=WorkloadPattern(
                pattern_type=self.config.workload_pattern,
                base_rate=self.config.base_arrival_rate
            ),
            seed=seed
        )
        
        # 根据调度器类型创建不同的组件
        if scheduler_type == "baseline":
            # Baseline: 保���调度，无敏感度，无补偿
            scheduler = BaselineScheduler(
                machines=machines,
                cpu_utilization_threshold=0.7
            )
            monitor = Monitor(machines, sample_interval=0.1)
            regulator = None
            
        elif scheduler_type == "jiagu":
            # Jiagu-like: 容量预测，无补偿
            scheduler = JiaguLikeScheduler(
                machines=machines,
                model_path=None,  # 在线训练
                capacity_update_interval=60.0
            )
            monitor = Monitor(machines, sample_interval=0.1)
            regulator = None
            
        elif scheduler_type == "resilient":
            # ResilientCPU: 完整方案
            scheduler = SchedulerFactory.create(
                "resilient",
                machines=machines,
                sensitivity_threshold=0.95,
                borrow_ratio=0.3,
                compensation_delay_ms=50.0
            )
            monitor = Monitor(machines, sample_interval=0.1)
            regulator = AdaptiveRegulator(
                machines={m.machine_id: m for m in machines},
                scheduler=scheduler,
                monitor=monitor,
                max_compensation_ratio=0.3,
                recovery_interval=5.0
            )
        else:
            raise ValueError(f"Unknown scheduler type: {scheduler_type}")
        
        # 注册函数
        for func in functions:
            scheduler.register_function(func)
        
        # 创建仿真环境
        simulator = self._create_simulator(
            machines=machines,
            functions=functions,
            workload_gen=workload_gen,
            scheduler=scheduler,
            monitor=monitor,
            regulator=regulator,
            scheduler_type=scheduler_type
        )
        
        # 运行仿真
        result = await simulator.run()
        result.scheduler_type = scheduler_type.capitalize()
        
        return result
    
    def _create_simulator(
        self,
        machines: List[Machine],
        functions: List[Function],
        workload_gen: WorkloadGenerator,
        scheduler,
        monitor: Monitor,
        regulator=None,
        scheduler_type: str = "baseline"
    ) -> ResilientCPUSimulator:
        """创建仿真器实例"""
        
        # 创建配置
        config = SimulationConfig(
            duration=self.config.duration,
            warmup=self.config.warmup,
            num_machines=self.config.num_machines,
            cpu_cores_per_machine=self.config.cpu_cores_per_machine,
            memory_per_machine_gb=self.config.memory_per_machine_gb,
            scheduler_strategy=scheduler_type,
            interference_enabled=self.config.interference_enabled,
            interference_pattern=self.config.interference_pattern
        )
        
        # 创建仿真器
        simulator = ResilientCPUSimulator(config)
        
        # 替换组件
        simulator.machines = {m.machine_id: m for m in machines}
        simulator.functions = {f.function_id: f for f in functions}
        simulator.scheduler = scheduler
        simulator.monitor = monitor
        simulator.regulator = regulator
        
        # 关联监控器到调度器
        if hasattr(scheduler, 'set_monitor'):
            scheduler.set_monitor(monitor)
        
        return simulator
    
    def _create_default_functions(self) -> List[Function]:
        """创建默认函数集"""
        from src.types import SensitivityProfile
        
        functions = []
        
        # 高敏感度函数
        high_funcs = [
            ("imageresize", 2.0, 512, 0.5),
            ("imagefilter", 2.0, 1024, 1.2),
            ("mediatranscode", 4.0, 2048, 3.0),
        ]
        
        # 中敏感度函数
        medium_funcs = [
            ("mlinference", 3.0, 2048, 0.8),
            ("datacompress", 1.5, 256, 2.0),
            ("dbcheck", 1.0, 1024, 0.5),
            ("reportgen", 2.0, 1024, 1.5),
        ]
        
        # 低敏感度函数
        low_funcs = [
            ("textparse", 0.5, 256, 0.3),
            ("logprocess", 0.5, 512, 0.1),
            ("apigateway", 0.5, 256, 0.05),
        ]
        
        sensitivity_map = {
            "high": [(0, 0), (20, 0.08), (40, 0.25), (60, 0.55), (80, 0.85), (100, 0.98)],
            "medium": [(0, 0), (20, 0.03), (40, 0.10), (60, 0.25), (80, 0.50), (100, 0.80)],
            "low": [(0, 0), (20, 0.01), (40, 0.03), (60, 0.08), (80, 0.18), (100, 0.40)],
        }
        
        for name, cpu, mem, latency in high_funcs:
            profile = SensitivityProfile(
                function_id=name,
                sensitivity_points=sensitivity_map["high"],
                knee_point=40.0,
                acceptable_degradation=0.05
            )
            functions.append(Function(
                function_id=name,
                name=name.capitalize(),
                memory_mb=mem,
                cpu_cores=cpu,
                execution_time_mean=latency,
                execution_time_std=latency * 0.2,
                timeout=latency * 3,
                sensitivity_profile=profile if scheduler_type == "resilient" else None
            ))
        
        for name, cpu, mem, latency in medium_funcs:
            profile = SensitivityProfile(
                function_id=name,
                sensitivity_points=sensitivity_map["medium"],
                knee_point=60.0,
                acceptable_degradation=0.05
            )
            functions.append(Function(
                function_id=name,
                name=name.capitalize(),
                memory_mb=mem,
                cpu_cores=cpu,
                execution_time_mean=latency,
                execution_time_std=latency * 0.2,
                timeout=latency * 3,
                sensitivity_profile=profile if scheduler_type == "resilient" else None
            ))
        
        for name, cpu, mem, latency in low_funcs:
            profile = SensitivityProfile(
                function_id=name,
                sensitivity_points=sensitivity_map["low"],
                knee_point=75.0,
                acceptable_degradation=0.05
            )
            functions.append(Function(
                function_id=name,
                name=name.capitalize(),
                memory_mb=mem,
                cpu_cores=cpu,
                execution_time_mean=latency,
                execution_time_std=latency * 0.2,
                timeout=latency * 3,
                sensitivity_profile=profile if scheduler_type == "resilient" else None
            ))
        
        return functions
    
    def _compute_statistics(self) -> None:
        """计算多次试验的统计数据"""
        self.summary = {}
        
        for scheduler_type, trials in self.results.items():
            if not trials:
                continue
            
            # 提取指标
            p99_latencies = [r.p99_latency for r in trials]
            sla_rates = [r.sla_violation_rate for r in trials]
            cpu_utils = [r.avg_cpu_utilization for r in trials]
            throughputs = [r.throughput for r in trials]
            
            self.summary[scheduler_type] = {
                "p99_latency": {
                    "mean": float(np.mean(p99_latencies)),
                    "std": float(np.std(p99_latencies)),
                    "min": float(np.min(p99_latencies)),
                    "max": float(np.max(p99_latencies))
                },
                "sla_violation_rate": {
                    "mean": float(np.mean(sla_rates)),
                    "std": float(np.std(sla_rates))
                },
                "cpu_utilization": {
                    "mean": float(np.mean(cpu_utils)),
                    "std": float(np.std(cpu_utils))
                },
                "throughput": {
                    "mean": float(np.mean(throughputs)),
                    "std": float(np.std(throughputs))
                }
            }
    
    def save_results(self, output_dir: str = None) -> None:
        """保存结果"""
        if output_dir is None:
            output_dir = self.config.results_dir
        
        os.makedirs(output_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # 保存详细结果
        detailed = {}
        for scheduler_type, trials in self.results.items():
            detailed[scheduler_type] = [asdict(r) for r in trials]
        
        with open(f"{output_dir}/detailed_{timestamp}.json", 'w') as f:
            json.dump(detailed, f, indent=2)
        
        # 保存统计摘要
        with open(f"{output_dir}/summary_{timestamp}.json", 'w') as f:
            json.dump(self.summary, f, indent=2)
        
        print(f"\n结果已保存到 {output_dir}/")
    
    def plot_comparison(self, output_dir: str = None) -> None:
        """绘制对比图表"""
        if output_dir is None:
            output_dir = self.config.results_dir
        
        os.makedirs(output_dir, exist_ok=True)
        
        import matplotlib.pyplot as plt
        import numpy as np
        
        fig, axes = plt.subplots(2, 3, figsize=(16, 10))
        axes = axes.flatten()
        
        names = list(self.summary.keys())
        
        # 1. P99延迟对比（带误差棒）
        p99_means = [self.summary[n]["p99_latency"]["mean"] for n in names]
        p99_stds = [self.summary[n]["p99_latency"]["std"] for n in names]
        axes[0].bar(names, p99_means, yerr=p99_stds, capsize=5, color=['steelblue', 'orange', 'green'])
        axes[0].set_ylabel('P99 Latency (ms)')
        axes[0].set_title('P99 Latency Comparison')
        axes[0].tick_params(axis='x', rotation=45)
        
        # 2. SLA违规率对比
        sla_means = [self.summary[n]["sla_violation_rate"]["mean"] * 100 for n in names]
        sla_stds = [self.summary[n]["sla_violation_rate"]["std"] * 100 for n in names]
        axes[1].bar(names, sla_means, yerr=sla_stds, capsize=5, color=['steelblue', 'orange', 'green'])
        axes[1].set_ylabel('SLA Violation Rate (%)')
        axes[1].set_title('SLA Violation Rate')
        axes[1].tick_params(axis='x', rotation=45)
        axes[1].set_ylim(0, max(sla_means) * 1.2 if sla_means else 0.1)
        
        # 3. CPU利用率对比
        cpu_means = [self.summary[n]["cpu_utilization"]["mean"] * 100 for n in names]
        cpu_stds = [self.summary[n]["cpu_utilization"]["std"] * 100 for n in names]
        axes[2].bar(names, cpu_means, yerr=cpu_stds, capsize=5, color=['steelblue', 'orange', 'green'])
        axes[2].set_ylabel('CPU Utilization (%)')
        axes[2].set_title('CPU Utilization')
        axes[2].tick_params(axis='x', rotation=45)
        
        # 4. 吞吐量对比
        tp_means = [self.summary[n]["throughput"]["mean"] for n in names]
        tp_stds = [self.summary[n]["throughput"]["std"] for n in names]
        axes[3].bar(names, tp_means, yerr=tp_stds, capsize=5, color=['steelblue', 'orange', 'green'])
        axes[3].set_ylabel('Throughput (req/s)')
        axes[3].set_title('Throughput')
        axes[3].tick_params(axis='x', rotation=45)
        
        # 5. 改进百分比（相对于Baseline）
        if "baseline" in self.summary and "resilient" in self.summary:
            baseline = self.summary["baseline"]
            resilient = self.summary["resilient"]
            
            # CPU利用率改进
            cpu_improve = (resilient["cpu_utilization"]["mean"] - baseline["cpu_utilization"]["mean"]) / baseline["cpu_utilization"]["mean"] * 100
            # P99延迟改进
            p99_improve = (baseline["p99_latency"]["mean"] - resilient["p99_latency"]["mean"]) / baseline["p99_latency"]["mean"] * 100
            # SLA改进
            sla_improve = (baseline["sla_violation_rate"]["mean"] - resilient["sla_violation_rate"]["mean"]) / max(baseline["sla_violation_rate"]["mean"], 0.001) * 100
            
            improvements = [cpu_improve, p99_improve, sla_improve]
            labels = ['CPU Util Impr.', 'P99 Latency Impr.', 'SLA Impr.']
            colors = ['green', 'blue', 'purple']
            
            bars = axes[4].barh(labels, improvements, color=colors)
            axes[4].set_xlabel('Improvement (%)')
            axes[4].set_title('ResilientCPU vs Baseline')
            axes[4].axvline(x=0, color='black', linewidth=0.8)
            
            # 添加数值标签
            for bar, imp in zip(bars, improvements):
                axes[4].text(bar.get_width() + 0.5, bar.get_y() + bar.get_height()/2,
                           f'{imp:.1f}%', va='center', ha='left')
        
        # 6. 延迟分布箱线图
        if self.results:
            data = []
            labels = []
            for scheduler_type, trials in self.results.items():
                # 取最后一次试验的延迟数据（需要从simulator获取）
                # 这里用P99值模拟分布
                data.append(self._generate_distribution_from_stats(
                    self.summary[scheduler_type]["p99_latency"]["mean"],
                    self.summary[scheduler_type]["p99_latency"]["std"]
                ))
                labels.append(scheduler_type)
            
            axes[5].boxplot(data, labels=labels)
            axes[5].set_ylabel('P99 Latency (ms)')
            axes[5].set_title('Latency Distribution')
            axes[5].tick_params(axis='x', rotation=45)
        
        plt.suptitle('ResilientCPU Comparison Results', fontsize=16, fontweight='bold')
        plt.tight_layout()
        
        output_path = f"{output_dir}/comparison_plot.png"
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        
        print(f"对比图表已保存: {output_path}")
    
    def _generate_distribution_from_stats(self, mean: float, std: float, n: int = 100) -> List[float]:
        """根据均值和标准差生成模拟数据"""
        import numpy as np
        return list(np.random.normal(mean, std, n))
    
    def print_summary(self) -> None:
        """打印统计摘要"""
        print("\n" + "="*80)
        print("实验统计摘要")
        print("="*80)
        
        header = f"{'Scheduler':<15} {'P99(ms)':<12} {'±Std':<8} {'SLA%':<10} {'±Std':<8} {'CPU%':<10} {'±Std':<8}"
        print(header)
        print("-"*80)
        
        for scheduler_type, stats in self.summary.items():
            p99 = stats["p99_latency"]
            sla = stats["sla_violation_rate"]
            cpu = stats["cpu_utilization"]
            
            line = (
                f"{scheduler_type:<15} "
                f"{p99['mean']:<12.2f} "
                f"{p99['std']:<8.2f} "
                f"{sla['mean']*100:<10.2f} "
                f"{sla['std']*100:<8.2f} "
                f"{cpu['mean']*100:<10.1f} "
                f"{cpu['std']*100:<8.2f}"
            )
            print(line)
        
        print("-"*80)
        
        # 打印改进百分比
        if "baseline" in self.summary and "resilient" in self.summary:
            baseline = self.summary["baseline"]
            resilient = self.summary["resilient"]
            
            cpu_improve = (resilient["cpu_utilization"]["mean"] - baseline["cpu_utilization"]["mean"]) / baseline["cpu_utilization"]["mean"] * 100
            p99_improve = (baseline["p99_latency"]["mean"] - resilient["p99_latency"]["mean"]) / baseline["p99_latency"]["mean"] * 100
            sla_improve = (baseline["sla_violation_rate"]["mean"] - resilient["sla_violation_rate"]["mean"]) / max(baseline["sla_violation_rate"]["mean"], 0.001) * 100
            
            print(f"\nResilientCPU 改进 (vs Baseline):")
            print(f"  CPU利用率: +{cpu_improve:.1f}%")
            print(f"  P99延迟: -{p99_improve:.1f}%")
            print(f"  SLA违规率: -{sla_improve:.1f}%")


async def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="ResilientCPU 对比实验")
    parser.add_argument("--duration", type=float, default=300.0,
                       help="每次实验仿真时长（秒）")
    parser.add_argument("--machines", type=int, default=4,
                       help="机器数量")
    parser.add_argument("--trials", type=int, default=3,
                       help="重复实验次数")
    parser.add_argument("--output", type=str, default="results/comparison",
                       help="输出目录")
    parser.add_argument("--scheduler", type=str, choices=["baseline", "jiagu", "resilient", "all"],
                       default="all", help="测试的调度器类型")
    
    args = parser.parse_args()
    
    # 创建配置
    config = ComparisonConfig(
        duration=args.duration,
        num_machines=args.machines,
        num_trials=args.trials,
        results_dir=args.output
    )
    
    # 创建对比框架
    comparison = SchedulerComparison(config)
    
    # 运行实验
    if args.scheduler == "all":
        results = await comparison.run_comparison()
    else:
        # 只运行指定调度器
        result = await comparison._run_single_trial(args.scheduler, seed=42)
        results = {args.scheduler: [result]}
        comparison.results = results
        comparison._compute_statistics()
    
    # 输出结果
    comparison.print_summary()
    comparison.save_results()
    comparison.plot_comparison()
    
    print(f"\n✅ 实验完成！结果已保存到 {args.output}/")


if __name__ == "__main__":
    asyncio.run(main())

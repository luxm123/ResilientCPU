#!/usr/bin/env python3
"""
ResilientCPU 实验运行脚本

运行对比实验：
1. Baseline: 传统保守调度（无敏感度信息）
2. Aggressive: 激进超卖（无视敏感度）
3. ResilientCPU: 我们的方案
"""

import asyncio
import sys
import os
import yaml
import json
import argparse
from datetime import datetime
from typing import Dict, List

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.simulator import ResilientCPUSimulator, SimulationConfig
from src.scheduler import ResilientScheduler
from src.workload_generator import WorkloadGenerator, WorkloadPattern
from src.types import ExperimentResult


def load_config(config_path: str) -> dict:
    """加载配置文件"""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def run_baseline_experiment(config: dict) -> ExperimentResult:
    """Baseline: 传统保守调度（无敏感度信息）"""
    print("\n" + "="*60)
    print("运行 Baseline (Conservative) 实验")
    print("="*60)

    sim_config = SimulationConfig(
        duration=config.get("duration", 300),
        warmup=config.get("warmup", 30),
        num_machines=config.get("num_machines", 4),
        cpu_cores_per_machine=config.get("cpu_cores", 8),
        scheduler_strategy="conservative",  # 保守策略，但不用敏感度
        interference_enabled=config.get("interference_enabled", True),
        interference_pattern=config.get("interference_pattern", "burst")
    )

    simulator = ResilientCPUSimulator(sim_config)
    result = asyncio.run(simulator.run())
    result.scheduler_type = "Baseline (Conservative)"

    return result, simulator


def run_aggressive_experiment(config: dict) -> ExperimentResult:
    """Aggressive: 激进超卖（无补偿机制）"""
    print("\n" + "="*60)
    print("运行 Aggressive 实验")
    print("="*60)

    sim_config = SimulationConfig(
        duration=config.get("duration", 300),
        warmup=config.get("warmup", 30),
        num_machines=config.get("num_machines", 4),
        cpu_cores_per_machine=config.get("cpu_cores", 8),
        scheduler_strategy="aggressive",
        interference_enabled=config.get("interference_enabled", True),
        interference_pattern=config.get("interference_pattern", "burst")
    )

    simulator = ResilientCPUSimulator(sim_config)
    result = asyncio.run(simulator.run())
    result.scheduler_type = "Aggressive (Overprovision)"

    return result, simulator


def run_resilient_experiment(config: dict) -> ExperimentResult:
    """ResilientCPU: 我们的完整方案"""
    print("\n" + "="*60)
    print("运行 ResilientCPU 实验")
    print("="*60)

    sim_config = SimulationConfig(
        duration=config.get("duration", 300),
        warmup=config.get("warmup", 30),
        num_machines=config.get("num_machines", 4),
        cpu_cores_per_machine=config.get("cpu_cores", 8),
        scheduler_strategy="resilient",
        sensitivity_threshold=config.get("sensitivity_threshold", 0.95),
        interference_enabled=config.get("interference_enabled", True),
        interference_pattern=config.get("interference_pattern", "burst")
    )

    simulator = ResilientCPUSimulator(sim_config)
    result = asyncio.run(simulator.run())
    result.scheduler_type = "ResilientCPU (Ours)"

    return result, simulator


def run_all_experiments(config_path: str) -> Dict[str, ExperimentResult]:
    """运行所有对比实验"""
    config = load_config(config_path)
    results = {}

    print("\n" + "="*60)
    print("ResilientCPU 实验套件")
    print("="*60)
    print(f"配置: {config}")

    # 运行三个实验
    try:
        results["Baseline"], _ = run_baseline_experiment(config)
        results["Aggressive"], _ = run_aggressive_experiment(config)
        results["ResilientCPU"], simulator_resilient = run_resilient_experiment(config)

        # 保存ResilientCPU的详细结果
        simulator_resilient.save_results()
        simulator_resilient.plot_results()

    except KeyboardInterrupt:
        print("\n实验被中断")
        return results
    except Exception as e:
        print(f"\n实验运行失败: {e}")
        import traceback
        traceback.print_exc()
        return results

    return results


def compare_results(results: Dict[str, ExperimentResult]) -> None:
    """对比和打印实验结果"""
    print("\n" + "="*80)
    print("实验结果对比")
    print("="*80)

    # 表头
    print(f"{'方案':<20} {'P99延迟(ms)':<15} {'SLA违规率':<12} {'吞吐量(req/s)':<15} {'CPU利用率':<12}")
    print("-"*80)

    for name, result in results.items():
        print(
            f"{name:<20} "
            f"{result.p99_latency:<15.2f} "
            f"{result.sla_violation_rate*100:<12.2f}% "
            f"{result.throughput:<15.2f} "
            f"{result.avg_cpu_utilization*100:<12.1f}%"
        )

    print("-"*80)

    # 计算改进
    if "ResilientCPU" in results and "Baseline" in results:
        r = results["ResilientCPU"]
        b = results["Baseline"]
        improvement_p99 = (b.p99_latency - r.p99_latency) / b.p99_latency * 100
        improvement_sla = (b.sla_violation_rate - r.sla_violation_rate) / b.sla_violation_rate * 100 if b.sla_violation_rate > 0 else 0

        print(f"\nResilientCPU 相比 Baseline:")
        print(f"  P99延迟改进: {improvement_p99:.1f}%")
        print(f"  SLA违规率改进: {improvement_sla:.1f}%")

    if "ResilientCPU" in results and "Aggressive" in results:
        r = results["ResilientCPU"]
        a = results["Aggressive"]
        improvement_p99 = (a.p99_latency - r.p99_latency) / a.p99_latency * 100
        improvement_sla = (a.sla_violation_rate - r.sla_violation_rate) / a.sla_violation_rate * 100

        print(f"\nResilientCPU 相比 Aggressive:")
        print(f"  P99延迟改进: {improvement_p99:.1f}%")
        print(f"  SLA违规率改进: {improvement_sla:.1f}%")


def save_results(results: Dict[str, ExperimentResult], output_dir: str = "results") -> None:
    """保存实验结果到文件"""
    import os
    import json
    from datetime import datetime

    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 保存为JSON
    output = {
        "timestamp": timestamp,
        "results": {}
    }

    for name, result in results.items():
        output["results"][name] = {
            "scheduler_type": result.scheduler_type,
            "avg_latency_ms": result.avg_latency,
            "p95_latency_ms": result.p95_latency,
            "p99_latency_ms": result.p99_latency,
            "sla_violation_rate": result.sla_violation_rate,
            "throughput": result.throughput,
            "avg_cpu_utilization": result.avg_cpu_utilization,
            "resource_efficiency": result.resource_efficiency,
            "latency_cv": result.latency_cv
        }

    output_path = f"{output_dir}/comparison_{timestamp}.json"
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\n实验结果已保存到: {output_path}")


def plot_comparison(results: Dict[str, ExperimentResult], output_dir: str = "results") -> None:
    """绘制对比图表"""
    import os
    import matplotlib.pyplot as plt
    import numpy as np

    os.makedirs(output_dir, exist_ok=True)

    fig, axes = plt.subplots(2, 3, figsize=(16, 10))

    names = list(results.keys())

    # 1. P99延迟对比
    p99_values = [results[n].p99_latency for n in names]
    axes[0, 0].bar(names, p99_values, color=['steelblue', 'orange', 'green'])
    axes[0, 0].set_ylabel('P99延迟 (ms)')
    axes[0, 0].set_title('P99延迟对比')
    axes[0, 0].tick_params(axis='x', rotation=45)

    # 2. SLA违规率对比
    sla_values = [results[n].sla_violation_rate * 100 for n in names]
    axes[0, 1].bar(names, sla_values, color=['steelblue', 'orange', 'green'])
    axes[0, 1].set_ylabel('违规率 (%)')
    axes[0, 1].set_title('SLA违规率对比')
    axes[0, 1].tick_params(axis='x', rotation=45)

    # 3. 吞吐量对比
    tp_values = [results[n].throughput for n in names]
    axes[0, 2].bar(names, tp_values, color=['steelblue', 'orange', 'green'])
    axes[0, 2].set_ylabel('吞吐量 (req/s)')
    axes[0, 2].set_title('吞吐量对比')
    axes[0, 2].tick_params(axis='x', rotation=45)

    # 4. CPU利用率对比
    cpu_values = [results[n].avg_cpu_utilization * 100 for n in names]
    axes[1, 0].bar(names, cpu_values, color=['steelblue', 'orange', 'green'])
    axes[1, 0].set_ylabel('利用率 (%)')
    axes[1, 0].set_title('CPU利用率对比')
    axes[1, 0].tick_params(axis='x', rotation=45)

    # 5. 资源效率对比
    eff_values = [results[n].resource_efficiency for n in names]
    axes[1, 1].bar(names, eff_values, color=['steelblue', 'orange', 'green'])
    axes[1, 1].set_ylabel('效率')
    axes[1, 1].set_title('资源效率对比')
    axes[1, 1].set_ylim(0, 1.0)
    axes[1, 1].tick_params(axis='x', rotation=45)

    # 6. 延迟变异系数对比
    cv_values = [results[n].latency_cv for n in names]
    axes[1, 2].bar(names, cv_values, color=['steelblue', 'orange', 'green'])
    axes[1, 2].set_ylabel('CV')
    axes[1, 2].set_title('延迟稳定性对比')
    axes[1, 2].tick_params(axis='x', rotation=45)

    plt.suptitle('ResilientCPU 对比实验结果', fontsize=16)
    plt.tight_layout()

    output_path = f"{output_dir}/comparison_plot.png"
    plt.savefig(output_path, dpi=150)
    plt.close()

    print(f"对比图表已保存到: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="ResilientCPU 实验运行器")
    parser.add_argument("--config", type=str, default="configs/experiment.yaml",
                       help="实验配置文件路径")
    parser.add_argument("--output", type=str, default="results",
                       help="输出目录")
    parser.add_argument("--baseline", action="store_true",
                       help="只运行Baseline实验")
    parser.add_argument("--aggressive", action="store_true",
                       help="只运行Aggressive实验")
    parser.add_argument("--resilient", action="store_true",
                       help="只运行ResilientCPU实验")

    args = parser.parse_args()

    # 加载配置
    config_path = args.config
    if not os.path.exists(config_path):
        print(f"配置文件不存在: {config_path}, 使用默认配置")
        config = {
            "duration": 300,      # 5分钟仿真
            "warmup": 30,
            "num_machines": 4,
            "cpu_cores": 8,
            "interference_enabled": True,
            "interference_pattern": "burst"
        }
    else:
        config = load_config(config_path)

    results = {}

    # 根据参数决定运行哪些实验
    if args.baseline:
        results["Baseline"], _ = run_baseline_experiment(config)
    elif args.aggressive:
        results["Aggressive"], _ = run_aggressive_experiment(config)
    elif args.resilient:
        results["ResilientCPU"], sim = run_resilient_experiment(config)
        sim.save_results()
        sim.plot_results()
    else:
        # 运行全部
        results = run_all_experiments(config_path if os.path.exists(config_path) else None)

    # 对比和保存
    if len(results) > 1:
        compare_results(results)
        plot_comparison(results, args.output)
        save_results(results, args.output)


if __name__ == "__main__":
    main()

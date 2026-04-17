#!/usr/bin/env python3
"""
快速运行脚本 - Quick Start

一键运行ResilientCPU仿真和对比实验
"""

import asyncio
import sys
import os
import argparse

# 添加src路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from simulator import ResilientCPUSimulator, SimulationConfig
from compare_schedulers import SchedulerComparison, ComparisonConfig


def quick_test():
    """快速测试：运行1分钟仿真"""
    print("\n" + "="*60)
    print("🚀 ResilientCPU 快速测试")
    print("="*60)
    
    config = SimulationConfig(
        duration=60.0,      # 1分钟
        warmup=5.0,
        num_machines=2,
        cpu_cores_per_machine=4,
        scheduler_strategy="resilient",
        interference_enabled=True,
        interference_pattern="burst"
    )
    
    simulator = ResilientCPUSimulator(config)
    
    try:
        result = asyncio.run(simulator.run())
        
        print("\n" + "="*60)
        print("📊 测试结果")
        print("="*60)
        print(f"  平均延迟:     {result.avg_latency:.2f} ms")
        print(f"  P95延迟:      {result.p95_latency:.2f} ms")
        print(f"  P99延迟:      {result.p99_latency:.2f} ms")
        print(f"  SLA违规率:    {result.sla_violation_rate*100:.2f}%")
        print(f"  吞吐量:       {result.throughput:.2f} req/s")
        print(f"  CPU利用率:    {result.avg_cpu_utilization*100:.1f}%")
        print("="*60)
        
        # 保存结果
        simulator.save_results("results")
        simulator.plot_results("results")
        
        print("\n✅ 测试完成！结果已保存到 results/ 目录")
        
    except KeyboardInterrupt:
        print("\n❌ 测试被中断")
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


def full_comparison():
    """完整对比实验：三种策略对比"""
    print("\n" + "="*60)
    print("🔬 完整对比实验")
    print("="*60)
    print("将对比三种调度策略：Baseline, Jiagu-like, ResilientCPU")
    print("每种策略运行3次，每次仿真5分钟")
    print("="*60)
    
    config = ComparisonConfig(
        duration=300.0,     # 5分钟
        warmup=30.0,
        num_machines=4,
        cpu_cores_per_machine=8,
        base_arrival_rate=50.0,
        interference_enabled=True,
        interference_pattern="burst",
        num_trials=3,
        results_dir="results/comparison"
    )
    
    comparison = SchedulerComparison(config)
    
    try:
        results = asyncio.run(comparison.run_comparison())
        
        # 打印摘要
        comparison.print_summary()
        
        # 保存和绘图
        comparison.save_results()
        comparison.plot_comparison()
        
        print("\n✅ 对比实验完成！")
        print("   详细结果: results/comparison/")
        
    except KeyboardInterrupt:
        print("\n❌ 实验被中断")
    except Exception as e:
        print(f"\n❌ 实验失败: {e}")
        import traceback
        traceback.print_exc()


def run_single_scheduler(scheduler_type: str, duration: float = 300.0):
    """运行单个调度器"""
    print(f"\n{'='*60}")
    print(f"🧪 测试调度器: {scheduler_type.upper()}")
    print('='*60)
    
    config = SimulationConfig(
        duration=duration,
        warmup=30.0,
        num_machines=4,
        cpu_cores_per_machine=8,
        scheduler_strategy=scheduler_type,
        interference_enabled=True,
        interference_pattern="burst"
    )
    
    simulator = ResilientCPUSimulator(config)
    
    try:
        result = asyncio.run(simulator.run())
        
        print(f"\n📊 {scheduler_type.upper()} 结果:")
        print(f"  P99延迟:      {result.p99_latency:.2f} ms")
        print(f"  SLA违规率:    {result.sla_violation_rate*100:.2f}%")
        print(f"  CPU利用率:    {result.avg_cpu_utilization*100:.1f}%")
        
        # 保存
        output_dir = f"results/{scheduler_type}"
        simulator.save_results(output_dir)
        simulator.plot_results(output_dir)
        
    except Exception as e:
        print(f"❌ 失败: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="ResilientCPU 快速运行脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s quick         快速测试（1分钟）
  %(prog)s compare       完整对比实验（3次×5分钟）
  %(prog)s baseline      单独测试Baseline
  %(prog)s jiagu         单独测试Jiagu-like
  %(prog)s resilient     单独测试ResilientCPU
        """
    )
    
    parser.add_argument(
        'mode',
        choices=['quick', 'compare', 'baseline', 'jiagu', 'resilient'],
        help='运行模式'
    )
    parser.add_argument(
        '--duration', type=float, default=300.0,
        help='仿真时长（秒），仅对single模式有效'
    )
    
    args = parser.parse_args()
    
    # 确保结果目录存在
    os.makedirs("results", exist_ok=True)
    
    if args.mode == 'quick':
        quick_test()
    elif args.mode == 'compare':
        full_comparison()
    elif args.mode in ['baseline', 'jiagu', 'resilient']:
        run_single_scheduler(args.mode, args.duration)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

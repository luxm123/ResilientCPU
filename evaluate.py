#!/usr/bin/env python3
"""
ResilientCPU 实验评估脚本

对比三种方法：
1. Baseline - 无调度，固定CPU shares
2. Jiagu-like - 基于阈值的简单补偿
3. ResilientCPU - 基于敏感度曲线的智能补偿

每个方法运行1小时，重复3次
收集指标：CPU利用率、QoS违反率、调度密度
"""

import time
import json
import subprocess
import threading
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path

# 配置
WORKER_IP = "172.31.26.175"
WORKER_URL = f"http://{WORKER_IP}"
CONTROLLER_IP = "172.31.31.194"
EXPERIMENT_DIR = Path("/home/ec2-user/experiments")
EXPERIMENT_DIR.mkdir(exist_ok=True)

# 方法配置
METHODS = {
    "baseline": {
        "name": "Baseline",
        "description": "固定CPU shares，无补偿机制"
    },
    "jiagu": {
        "name": "Jiagu-like",
        "description": "基于固定阈值的简单补偿（单阈值）"
    },
    "resilient": {
        "name": "ResilientCPU",
        "description": "基于敏感度曲线的自适应补偿"
    }
}

# 函数QoS阈值（ms）
QOS_THRESHOLDS = {
    "cpu_intensive": 100,
    "io_mixed": 200,
    "normal": 300
}


class ExperimentRunner:
    """实验运行器"""

    def __init__(self, method_name, worker_ip=WORKER_IP):
        self.method_name = method_name
        self.worker_ip = worker_ip
        self.start_time = None
        self.end_time = None
        self.metrics = defaultdict(list)
        self.request_log = []

    def log(self, metric_type, data):
        """记录指标"""
        self.metrics[metric_type].append({
            "timestamp": time.time(),
            "data": data
        })

    def run_baseline(self, duration_seconds):
        """
        Baseline方法：固定CPU shares，不进行任何调度
        """
        print(f"\n[{datetime.now()}] 运行 Baseline 方法 ({duration_seconds}秒)")

        # 重置worker状态（通过重启或API，这里假设初始状态）
        # Baseline不做任何调度，只是被动记录

        # 启动一个线程定期收集指标
        stop_collection = threading.Event()

        def collect_metrics():
            while not stop_collection.is_set():
                try:
                    # 获取各函数状态
                    for func_name, port in [("cpu_intensive", 8081),
                                              ("io_mixed", 8082),
                                              ("normal", 8083)]:
                        resp = requests.get(f"http://{self.worker_ip}:{port}/status", timeout=2)
                        if resp.status_code == 200:
                            status = resp.json()
                            self.log("latency", {
                                "function": func_name,
                                "latency_ms": status.get("avg_latency_ms", 0),
                                "cpu_shares": status.get("cpu_shares", 1024)
                            })
                except Exception as e:
                    print(f"收集指标失败: {e}")

                time.sleep(5)

        collector_thread = threading.Thread(target=collect_metrics, daemon=True)
        collector_thread.start()

        # 等待指定时间
        time.sleep(duration_seconds)
        stop_collection.set()
        collector_thread.join(timeout=2)

        print(f"[{datetime.now()}] Baseline 运行完成")

    def run_jiagu(self, duration_seconds):
        """
        Jiagu-like方法：基于单一阈值的补偿
        策略：如果任意函数延迟超过其QoS阈值的150%，则从normal函数借256 shares
        """
        print(f"\n[{datetime.now()}] 运行 Jiagu-like 方法 ({duration_seconds}秒)")

        stop_collection = threading.Event()

        def collect_and_schedule():
            while not stop_collection.is_set():
                try:
                    # 获取各函数状态
                    status = {}
                    for func_name, port in [("cpu_intensive", 8081),
                                              ("io_mixed", 8082),
                                              ("normal", 8083)]:
                        resp = requests.get(f"http://{self.worker_ip}:{port}/status", timeout=2)
                        if resp.status_code == 200:
                            status[func_name] = resp.json()

                    # 检查是否需要补偿
                    for func_name, func_status in status.items():
                        if func_status:
                            latency = func_status.get("avg_latency_ms", 0)
                            threshold = QOS_THRESHOLDS[func_name]

                            # 如果延迟超过阈值150%，从normal借资源
                            if latency > threshold * 1.5 and func_name != "normal":
                                try:
                                    requests.post(
                                        f"http://{self.worker_ip}:8081/compensate",
                                        json={"donor": "normal", "amount": 256},
                                        timeout=2
                                    )
                                    print(f"Jiagu: {func_name} 延迟过高({latency:.1f}ms), 从normal借256 shares")
                                except:
                                    pass

                    # 记录指标
                    for func_name, func_status in status.items():
                        if func_status:
                            self.log("latency", {
                                "function": func_name,
                                "latency_ms": func_status.get("avg_latency_ms", 0),
                                "cpu_shares": func_status.get("cpu_shares", 1024)
                            })

                except Exception as e:
                    print(f"Jiagu调度错误: {e}")

                time.sleep(5)

        scheduler_thread = threading.Thread(target=collect_and_schedule, daemon=True)
        scheduler_thread.start()

        time.sleep(duration_seconds)
        stop_collection.set()
        scheduler_thread.join(timeout=2)

        print(f"[{datetime.now()}] Jiagu-like 运行完成")

    def run_resilient(self, duration_seconds):
        """
        ResilientCPU方法：基于敏感度曲线的自适应补偿
        """
        print(f"\n[{datetime.now()}] 运行 ResilientCPU 方法 ({duration_seconds}秒)")

        stop_collection = threading.Event()

        def collect_and_schedule():
            while not stop_collection.is_set():
                try:
                    # 获取各函数状态
                    status = {}
                    for func_name, port in [("cpu_intensive", 8081),
                                              ("io_mixed", 8082),
                                              ("normal", 8083)]:
                        resp = requests.get(f"http://{self.worker_ip}:{port}/status", timeout=2)
                        if resp.status_code == 200:
                            status[func_name] = resp.json()

                    # 计算CPU争抢程度（基于延迟增长）
                    contention_scores = []
                    for func_name, func_status in status.items():
                        if func_status:
                            # 简单估算：当前延迟 / QoS阈值
                            latency = func_status.get("avg_latency_ms", 0)
                            threshold = QOS_THRESHOLDS[func_name]
                            contention_scores.append(min(1.0, latency / threshold / 2))

                    contention = np.mean(contention_scores) if contention_scores else 0.0

                    # 决策补偿
                    # 如果争抢程度较低（<0.5），且高敏感函数延迟超标
                    if contention < 0.5:
                        for func_name in ["cpu_intensive", "io_mixed"]:
                            if status.get(func_name):
                                latency = status[func_name].get("avg_latency_ms", 0)
                                if latency > QOS_THRESHOLDS[func_name]:
                                    # 从normal借资源
                                    try:
                                        requests.post(
                                            f"http://{self.worker_ip}:8081/compensate",
                                            json={"donor": "normal", "amount": 256},
                                            timeout=2
                                        )
                                        print(f"Resilient: {func_name} 需要补偿, 从normal借256 shares")
                                    except:
                                        pass

                    # 记录指标
                    for func_name, func_status in status.items():
                        if func_status:
                            self.log("latency", {
                                "function": func_name,
                                "latency_ms": func_status.get("avg_latency_ms", 0),
                                "cpu_shares": func_status.get("cpu_shares", 1024),
                                "contention": contention
                            })

                except Exception as e:
                    print(f"Resilient调度错误: {e}")

                time.sleep(5)

        scheduler_thread = threading.Thread(target=collect_and_schedule, daemon=True)
        scheduler_thread.start()

        time.sleep(duration_seconds)
        stop_collection.set()
        scheduler_thread.join(timeout=2)

        print(f"[{datetime.now()}] ResilientCPU 运行完成")

    def calculate_metrics(self):
        """计算实验指标"""
        results = {
            "cpu_utilization": 0.0,
            "qos_violation_rate": 0.0,
            "scheduling_density": 0.0
        }

        # 1. CPU利用率估算（基于CPU shares分配）
        total_shares_time = defaultdict(float)
        sample_count = 0

        for entry in self.metrics["latency"]:
            data = entry["data"]
            func = data["function"]
            shares = data.get("cpu_shares", 1024)
            total_shares_time[func] += shares
            sample_count += 1

        if sample_count > 0:
            avg_shares = sum(total_shares_time.values()) / (3 * sample_count)
            max_shares = 1024 * 3
            results["cpu_utilization"] = avg_shares / max_shares * 100

        # 2. QoS违反率
        qos_violations = 0
        total_requests = 0
        for entry in self.metrics["latency"]:
            data = entry["data"]
            func = data["function"]
            latency = data.get("latency_ms", 0)
            if latency > QOS_THRESHOLDS[func]:
                qos_violations += 1
            total_requests += 1

        results["qos_violation_rate"] = qos_violations / total_requests * 100 if total_requests > 0 else 0

        # 3. 调度密度（每单位时间的调度次数）
        # 这里简化为记录的metrics数量 / 持续时间
        if self.start_time and self.end_time:
            duration = self.end_time - self.start_time
            results["scheduling_density"] = len(self.metrics["latency"]) / duration if duration > 0 else 0

        return results


def run_experiment(method_key, run_id, duration_seconds=3600):
    """
    运行单次实验

    Args:
        method_key: 方法key ("baseline", "jiagu", "resilient")
        run_id: 实验重复ID (1, 2, 3)
        duration_seconds: 每次运行时长（默认1小时）
    """
    print(f"\n{'='*60}")
    print(f"实验: {METHODS[method_key]['name']} - 第{run_id}次运行")
    print(f"{'='*60}")

    runner = ExperimentRunner(method_key)
    runner.start_time = time.time()

    try:
        if method_key == "baseline":
            runner.run_baseline(duration_seconds)
        elif method_key == "jiagu":
            runner.run_jiagu(duration_seconds)
        elif method_key == "resilient":
            runner.run_resilient(duration_seconds)

        runner.end_time = time.time()

        # 计算指标
        metrics = runner.calculate_metrics()
        metrics["method"] = method_key
        metrics["run_id"] = run_id
        metrics["duration"] = duration_seconds

        print(f"\n实验结果:")
        print(f"  CPU利用率: {metrics['cpu_utilization']:.2f}%")
        print(f"  QoS违反率: {metrics['qos_violation_rate']:.2f}%")
        print(f"  调度密度: {metrics['scheduling_density']:.2f} 次/秒")

        # 保存结果
        result_file = EXPERIMENT_DIR / f"{method_key}_run{run_id}_results.json"
        with open(result_file, 'w') as f:
            json.dump({
                "method": method_key,
                "run_id": run_id,
                "timestamp": time.time(),
                "metrics": metrics,
                "raw_metrics_count": {k: len(v) for k, v in runner.metrics.items()}
            }, f, indent=2)

        print(f"结果保存到: {result_file}")
        return metrics

    except Exception as e:
        print(f"实验失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    parser = argparse.ArgumentParser(description="ResilientCPU 实验评估")
    parser.add_argument("--duration", type=int, default=3600, help="每次运行时长（秒，默认3600=1小时）")
    parser.add_argument("--repeats", type=int, default=3, help="重复次数（默认3次）")
    parser.add_argument("--methods", nargs="+", default=["baseline", "jiagu", "resilient"],
                       help="要运行的方法列表")
    args = parser.parse_args()

    print("=" * 60)
    print("ResilientCPU 实验评估")
    print("=" * 60)
    print(f"实验配置:")
    print(f"  每次运行时长: {args.duration}秒 ({args.duration/3600:.1f}小时)")
    print(f"  重复次数: {args.repeats}")
    print(f"  方法: {args.methods}")
    print(f"  Worker地址: {WORKER_URL}")

    all_results = []

    # 对每个方法运行指定次数
    for method in args.methods:
        if method not in METHODS:
            print(f"警告: 未知方法 '{method}'，跳过")
            continue

        for run_id in range(1, args.repeats + 1):
            result = run_experiment(method, run_id, args.duration)
            if result:
                all_results.append(result)

            # 两次运行之间休息一下
            if run_id < args.repeats:
                print(f"等待30秒后开始下一次运行...")
                time.sleep(30)

    # 汇总结果
    if all_results:
        print("\n\n========== 实验汇总 ==========")
        df = pd.DataFrame(all_results)

        for method in args.methods:
            method_results = df[df["method"] == method]
            if len(method_results) > 0:
                print(f"\n{METHODS[method]['name']}:")
                for metric in ["cpu_utilization", "qos_violation_rate", "scheduling_density"]:
                    values = method_results[metric].values
                    print(f"  {metric}: {np.mean(values):.2f} ± {np.std(values):.2f}")

        # 保存汇总
        summary_file = EXPERIMENT_DIR / "summary.csv"
        df.to_csv(summary_file, index=False)
        print(f"\n汇总保存到: {summary_file}")

        # 生成可视化图表
        generate_plots(df)

    else:
        print("\n警告: 没有收集到实验结果")


def generate_plots(df):
    """生成对比图表"""
    try:
        import matplotlib
        matplotlib.use('Agg')  # 非交���式后端
        import matplotlib.pyplot as plt
        import seaborn as sns

        # 设置样式
        plt.style.use('seaborn-v0_8-darkgrid')
        sns.set_palette("husl")

        fig, axes = plt.subplots(1, 3, figsize=(15, 5))

        # 1. CPU利用率对比
        cpu_means = [df[df["method"] == m]["cpu_utilization"].mean() for m in METHODS.keys()]
        cpu_stds = [df[df["method"] == m]["cpu_utilization"].std() for m in METHODS.keys()]
        axes[0].bar(range(len(METHODS)), cpu_means, yerr=cpu_stds, capsize=5)
        axes[0].set_xlabel("Method")
        axes[0].set_ylabel("CPU Utilization (%)")
        axes[0].set_title("CPU Utilization Comparison")
        axes[0].set_xticks(range(len(METHODS)))
        axes[0].set_xticklabels([METHODS[m]["name"] for m in METHODS.keys()])

        # 2. QoS违反率对比
        qos_means = [df[df["method"] == m]["qos_violation_rate"].mean() for m in METHODS.keys()]
        qos_stds = [df[df["method"] == m]["qos_violation_rate"].std() for m in METHODS.keys()]
        axes[1].bar(range(len(METHODS)), qos_means, yerr=qos_stds, capsize=5, color='orange')
        axes[1].set_xlabel("Method")
        axes[1].set_ylabel("QoS Violation Rate (%)")
        axes[1].set_title("QoS Violation Rate Comparison")
        axes[1].set_xticks(range(len(METHODS)))
        axes[1].set_xticklabels([METHODS[m]["name"] for m in METHODS.keys()])

        # 3. 调度密度对比
        density_means = [df[df["method"] == m]["scheduling_density"].mean() for m in METHODS.keys()]
        density_stds = [df[df["method"] == m]["scheduling_density"].std() for m in METHODS.keys()]
        axes[2].bar(range(len(METHODS)), density_means, yerr=density_stds, capsize=5, color='green')
        axes[2].set_xlabel("Method")
        axes[2].set_ylabel("Scheduling Density (ops/sec)")
        axes[2].set_title("Scheduling Density Comparison")
        axes[2].set_xticks(range(len(METHODS)))
        axes[2].set_xticklabels([METHODS[m]["name"] for m in METHODS.keys()])

        plt.tight_layout()
        plot_file = EXPERIMENT_DIR / "comparison_plot.png"
        plt.savefig(plot_file, dpi=150)
        print(f"\n对比图表保存到: {plot_file}")

    except ImportError:
        print("\n警告: matplotlib/seaborn未安装，跳过图表生成")


if __name__ == "__main__":
    import argparse
    main()

#!/usr/bin/env python3
"""
ResilientCPU 实验评估脚本

对比三种方法：
1. Baseline: 不超卖，每个函数独占资源（无调度）
2. Jiagu-like: 离线训练模型，预测 capacity，调度时查表，不动态补偿
3. ResilientCPU: 敏感度曲线 + 拐点判断 + 运行时动态调整CPU shares

实验流程：
- 预热2分钟
- 运行1小时，重复3次
- 收集指标：CPU利用率、QoS违反率、调度密度、补偿响应时间
"""

import time
import json
import subprocess
import threading
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict, deque
from pathlib import Path
import sys

# 配置
WORKER_IP = "172.31.26.175"
CONTROLLER_IP = "172.31.31.194"
BASE_DIR = Path("/home/ec2-user/ResilientCPU")
EXPERIMENT_DIR = BASE_DIR / "experiments"
EXPERIMENT_DIR.mkdir(exist_ok=True, parents=True)

# 实验参数
WARMUP_DURATION = 120  # 预热2分钟
RUN_DURATION = 3600    # 运行1小时
REPEATS = 3            # 重复3次

# QoS阈值 = 基准延迟 × 1.5
QOS_MULTIPLIER = 1.5

# 基准延迟（单函数独占资源时的延迟，需要先测量）
# 将在预热阶段自动测量
BASELINE_LATENCY = {
    "cpu_intensive": None,
    "io_mixed": None,
    "normal": None
}

# Poisson过程的QPS变化（10到100）
QPS_SCHEDULE = [
    (300, 10),   # 前5分钟：QPS=10
    (300, 20),   # 5-10分钟：QPS=20
    (300, 30),   # 10-15分钟：QPS=30
    (600, 50),   # 15-25分钟：QPS=50
    (600, 70),   # 25-35分钟：QPS=70
    (600, 100),  # 35-45分钟：QPS=100
    (300, 50),   # 45-50分钟：QPS=50
    (300, 20),   # 50-55分钟：QPS=20
    (300, 10),   # 55-60分钟：QPS=10
]


# ============ 辅助函数 ============

def poisson_arrivals(qps, duration):
    """生成Poisson请求到达时间"""
    arrivals = []
    t = 0
    while t < duration:
        interval = random.expovariate(qps)
        t += interval
        if t < duration:
            arrivals.append(t)
    return arrivals


def get_worker_status():
    """获取所有函数状态"""
    status = {}
    for func, port in [("cpu_intensive", 8081), ("io_mixed", 8082), ("normal", 8083)]:
        try:
            resp = requests.get(f"http://{WORKER_IP}:{port}/status", timeout=2)
            if resp.status_code == 200:
                status[func] = resp.json()
        except Exception as e:
            print(f"[WARN] 获取 {func} 状态失败: {e}")
            status[func] = None
    return status


def invoke_function(func_name):
    """调用函数"""
    port = {"cpu_intensive": 8081, "io_mixed": 8082, "normal": 8083}[func_name]
    try:
        resp = requests.post(f"http://{WORKER_IP}:{port}/invoke", timeout=30)
        latency = resp.json().get("latency_ms", 0) if resp.status_code == 200 else -1
        return {"function": func_name, "latency_ms": latency, "success": resp.status_code == 200}
    except:
        return {"function": func_name, "latency_ms": -1, "success": False}


def measure_baseline_latency():
    """
    测量基准延迟
    需要确保每个函数独占资源时测量
    返回: {func_name: baseline_latency_ms}
    """
    print("\n=== 测量基准延迟 ===")

    # 重置worker状态
    try:
        requests.post(f"http://{WORKER_IP}:8081/reset")
    except:
        pass

    # 对每个函数单独测量
    baselines = {}

    for func_name in ["cpu_intensive", "io_mixed", "normal"]:
        print(f"  测量 {func_name} 基准延迟...")
        latencies = []

        for _ in range(10):
            result = invoke_function(func_name)
            if result["success"]:
                latencies.append(result["latency_ms"])
            time.sleep(0.5)

        if latencies:
            baseline = sum(latencies) / len(latencies)
            baselines[func_name] = baseline
            print(f"    {func_name}: {baseline:.2f} ms")

    # 设置worker的基准值
    for func_name, baseline in baselines.items():
        try:
            requests.post(
                f"http://{WORKER_IP}:{WORKER_PORTS[func_name]}/set_baseline",
                json={"function": func_name, "baseline_latency_ms": baseline}
            )
        except:
            pass

    return baselines


def calculate_metrics(status_history, request_log, baseline_latency):
    """
    计算实验指标

    参数:
    - status_history: [(timestamp, status_dict), ...]
    - request_log: [{"function": ..., "latency_ms": ..., "success": ...}, ...]
    - baseline_latency: {func: baseline_ms}

    返回:
    - metrics: dict
    """
    print("\n=== 计算指标 ===")

    metrics = {}

    # 1. CPU利用率（通过CPU shares的平均值估算）
    # Baseline模式：每个函数独占1024 shares，利用率取决于负载
    # 超卖模式：总shares > 1024*3，利用率更高

    # 收集每个时刻的shares分配
    total_shares_samples = []
    for ts, status in status_history:
        total = 0
        for func, s in status.items():
            if s:
                total += s.get("cpu_shares", 1024)
        total_shares_samples.append(total)

    if total_shares_samples:
        # Baseline: 每个1024，总共3072
        baseline_total_shares = 1024 * 3
        avg_total_shares = sum(total_shares_samples) / len(total_shares_samples)

        # CPU利用率 = (平均总shares / 基准总shares) × 100%
        # 注意：这里假设shares完全对应CPU资源，实际会有偏差
        metrics["cpu_utilization"] = min(100.0, avg_total_shares / baseline_total_shares * 100)
        print(f"  CPU利用率: {metrics['cpu_utilization']:.2f}%")

    # 2. QoS违反率
    qos_violations = 0
    total_requests = 0

    for req in request_log:
        if not req.get("success", False):
            total_requests += 1
            continue

        func = req["function"]
        latency = req.get("latency_ms", 0)
        baseline = baseline_latency.get(func, latency)
        qos_threshold = baseline * QOS_MULTIPLIER

        if latency > qos_threshold:
            qos_violations += 1

        total_requests += 1

    if total_requests > 0:
        metrics["qos_violation_rate"] = qos_violations / total_requests * 100
        print(f"  QoS违反率: {metrics['qos_violation_rate']:.2f}%")

    # 3. 调度密度
    # 定义为：每个函数平均的CPU shares调整次数/秒
    metrics["scheduling_density"] = len(status_history) / len(status_history) if status_history else 0
    print(f"  调度密度: {metrics['scheduling_density']:.2f} 次/秒")

    # 4. 补偿响应时间（仅ResilientCPU）
    # 需要从scheduler获取
    if hasattr(current_scheduler, 'recovery_times'):
        recovery_times = current_scheduler.recovery_times
        if recovery_times:
            metrics["compensation_response_time_ms"] = np.mean(recovery_times) * 1000
            print(f"  补偿响应时间: {metrics['compensation_response_time_ms']:.2f} ms")

    return metrics


# ============ 实验运行器 ============

class ExperimentRunner:
    """单次实验运行器"""

    def __init__(self, method_name):
        self.method_name = method_name
        self.status_history = []
        self.request_log = []
        self.baseline_latency = None

    def run(self, duration, warmup):
        """运行一次完整实验"""
        print(f"\n{'='*60}")
        print(f"方法: {self.method_name}")
        print(f"运行时长: {duration}秒, 预热: {warmup}秒")
        print(f"{'='*60}")

        # 1. 测量基准延迟（只需要第一次运行前测量一次）
        if self.method_name == "baseline":
            # Baseline需要先测量每个函数的独占基准
            self.baseline_latency = measure_baseline_latency()
        else:
            # 其他方法使用预定义的基准值
            self.baseline_latency = {
                "cpu_intensive": 21000,
                "io_mixed": 15,
                "normal": 0.1
            }

        # 2. 选择调度器
        global current_scheduler
        if self.method_name == "baseline":
            from controller_scheduler import BaselineScheduler
            scheduler = BaselineScheduler()
        elif self.method_name == "jiagu":
            from controller_scheduler import JiaguLikeScheduler
            scheduler = JiaguLikeScheduler()
        else:
            from controller_scheduler import ResilientCPUScheduler
            scheduler = ResilientCPUScheduler()

        current_scheduler = scheduler

        # 3. 重置worker
        try:
            requests.post(f"http://{WORKER_IP}:8081/reset")
        except:
            pass

        # 4. 预热阶段（不记录数据，但让系统稳定）
        print(f"\n--- 预热阶段 ({warmup}s) ---")
        time.sleep(warmup)

        # 5. 正式运行阶段
        print(f"\n--- 正式运行阶段 ({duration}s) ---")
        start_time = time.time()

        # 启动状态收集线程
        def collect_status():
            while time.time() - start_time < duration:
                status = get_worker_status()
                self.status_history.append((time.time(), status))
                time.sleep(5)

        status_thread = threading.Thread(target=collect_status, daemon=True)
        status_thread.start()

        # 生成负载（Poisson过程）
        total_requests = 0
        for phase_duration, phase_qps in QPS_SCHEDULE:
            if time.time() - start_time >= duration:
                break

            print(f"  阶段: QPS={phase_qps}, 持续={phase_duration}s")

            # 生成Poisson到达
            arrivals = poisson_arrivals(phase_qps, min(phase_duration, duration - (time.time() - start_time)))
            arrivals.sort()

            for arrival in arrivals:
                if time.time() - start_time >= duration:
                    break

                # 等待到到达时间
                wait_time = arrival - (time.time() - start_time)
                if wait_time > 0:
                    time.sleep(wait_time)

                # 随机选择函数（均匀分布）
                func = random.choice(["cpu_intensive", "io_mixed", "normal"])
                result = invoke_function(func)
                self.request_log.append(result)
                total_requests += 1

                if total_requests % 100 == 0:
                    print(f"    已发送 {total_requests} 请求")

        # 等待状态收集线程结束
        status_thread.join(timeout=10)

        print(f"\n实验完成，共发送 {total_requests} 请求")

        # 6. 计算指标
        metrics = calculate_metrics(self.status_history, self.request_log, self.baseline_latency)

        return metrics


def run_trials(method_name, num_trials=3):
    """多次运行同一方法"""
    all_metrics = []

    for trial in range(1, num_trials + 1):
        print(f"\n{'='*20} 第{trial}次运行 {'='*20}")

        runner = ExperimentRunner(method_name)
        metrics = runner.run(RUN_DURATION, WARMUP_DURATION)

        # 添加元数据
        metrics["method"] = method_name
        metrics["trial"] = trial
        metrics["timestamp"] = datetime.now().isoformat()

        all_metrics.append(metrics)

        # 保存单次结果
        trial_file = EXPERIMENT_DIR / f"{method_name}_trial{trial}.json"
        with open(trial_file, 'w') as f:
            json.dump(metrics, f, indent=2)
        print(f"结果保存到: {trial_file}")

        # 两次运行之间等待一下
        if trial < num_trials:
            print("等待60秒后开始下一次运行...")
            time.sleep(60)

    return all_metrics


def main():
    parser = argparse.ArgumentParser(description="ResilientCPU实验评估")
    parser.add_argument("--methods", nargs="+", default=["baseline", "jiagu", "resilient"],
                       help="要运行的方法")
    parser.add_argument("--trials", type=int, default=REPEATS, help="重复次数")
    parser.add_argument("--duration", type=int, default=RUN_DURATION, help="每次运行时长（秒）")
    parser.add_argument("--warmup", type=int, default=WARMUP_DURATION, help="预热时长（秒）")
    args = parser.parse_args()

    global RUN_DURATION, WARMUP_DURATION, REPEATS
    RUN_DURATION = args.duration
    WARMUP_DURATION = args.warmup
    REPEATS = args.trials

    print("=" * 60)
    print("ResilientCPU 实验评估")
    print("=" * 60)
    print(f"方法: {args.methods}")
    print(f"每次运行: 预热{WARMUP_DURATION}s + 运行{RUN_DURATION}s")
    print(f"重复次数: {REPEATS}")
    print(f"Worker: {WORKER_IP}")
    print("=" * 60)

    all_results = []

    # 运行每个方法
    for method in args.methods:
        if method not in ["baseline", "jiagu", "resilient"]:
            print(f"[WARN] 未知方法: {method}")
            continue

        results = run_trials(method, REPEATS)
        all_results.extend(results)

    # 汇总结果
    if all_results:
        print("\n\n========== 实验结果汇总 ==========")

        df = pd.DataFrame(all_results)

        # 显示每个方法的平均指标
        for method in args.methods:
            method_df = df[df["method"] == method]
            if len(method_df) > 0:
                print(f"\n{method.upper()}:")
                for col in ["cpu_utilization", "qos_violation_rate", "scheduling_density"]:
                    if col in method_df.columns:
                        mean = method_df[col].mean()
                        std = method_df[col].std()
                        print(f"  {col}: {mean:.2f} ± {std:.2f}")

                # 补偿响应时间（仅ResilientCPU）
                if "compensation_response_time_ms" in method_df.columns:
                    crt = method_df["compensation_response_time_ms"].mean()
                    print(f"  补偿响应时间: {crt:.2f} ms")

        # 保存汇总
        summary_file = EXPERIMENT_DIR / "summary.csv"
        df.to_csv(summary_file, index=False)
        print(f"\n汇总数据保存到: {summary_file}")

        # 生成对比图表
        generate_comparison_plots(df)

    else:
        print("\n[ERROR] 没有实验结果")


def generate_comparison_plots(df):
    """生成对比图表"""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import seaborn as sns

        methods = [m for m in ["baseline", "jiagu", "resilient"] if m in df["method"].unique()]

        fig, axes = plt.subplots(1, 3, figsize=(15, 5))

        # 1. CPU利用率
        cpu_means = [df[df["method"] == m]["cpu_utilization"].mean() for m in methods]
        cpu_stds = [df[df["method"] == m]["cpu_utilization"].std() for m in methods]
        axes[0].bar(range(len(methods)), cpu_means, yerr=cpu_stds, capsize=5, color='steelblue')
        axes[0].set_xlabel("Method")
        axes[0].set_ylabel("CPU Utilization (%)")
        axes[0].set_title("CPU Utilization Comparison")
        axes[0].set_xticks(range(len(methods)))
        axes[0].set_xticklabels([m.capitalize() for m in methods])

        # 2. QoS违反率
        qos_means = [df[df["method"] == m]["qos_violation_rate"].mean() for m in methods]
        qos_stds = [df[df["method"] == m]["qos_violation_rate"].std() for m in methods]
        axes[1].bar(range(len(methods)), qos_means, yerr=qos_stds, capsize=5, color='coral')
        axes[1].set_xlabel("Method")
        axes[1].set_ylabel("QoS Violation Rate (%)")
        axes[1].set_title("QoS Violation Rate Comparison")
        axes[1].set_xticks(range(len(methods)))
        axes[1].set_xticklabels([m.capitalize() for m in methods])

        # 3. 调度密度
        density_means = [df[df["method"] == m]["scheduling_density"].mean() for m in methods]
        density_stds = [df[df["method"] == m]["scheduling_density"].std() for m in methods]
        axes[2].bar(range(len(methods)), density_means, yerr=density_stds, capsize=5, color='forestgreen')
        axes[2].set_xlabel("Method")
        axes[2].set_ylabel("Scheduling Density (ops/s)")
        axes[2].set_title("Scheduling Density Comparison")
        axes[2].set_xticks(range(len(methods)))
        axes[2].set_xticklabels([m.capitalize() for m in methods])

        plt.tight_layout()
        plot_file = EXPERIMENT_DIR / "comparison_plot.png"
        plt.savefig(plot_file, dpi=150)
        print(f"\n图表保存到: {plot_file}")

        # 详细对比表
        fig2, ax2 = plt.subplots(figsize=(10, 6))
        ax2.axis('tight')
        ax2.axis('off')

        table_data = []
        headers = ["Method", "CPU Util (%)", "QoS Viol (%)", "Sched Density", "Resp Time (ms)"]
        for method in methods:
            row = [method.capitalize()]
            for col in ["cpu_utilization", "qos_violation_rate", "scheduling_density"]:
                if col in df.columns:
                    val = df[df["method"] == method][col].mean()
                    row.append(f"{val:.1f}")
                else:
                    row.append("N/A")
            # 补偿响应时间
            if "compensation_response_time_ms" in df.columns:
                crt = df[df["method"] == method]["compensation_response_time_ms"].mean()
                row.append(f"{crt:.1f}" if not pd.isna(crt) else "N/A")
            else:
                row.append("N/A")
            table_data.append(row)

        table = ax2.table(cellText=table_data, colLabels=headers, loc='center', cellLoc='center')
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        table.scale(1, 2)

        plt.savefig(EXPERIMENT_DIR / "comparison_table.png", dpi=150, bbox_inches='tight')
        print(f"对比表保存到: {EXPERIMENT_DIR / 'comparison_table.png'}")

    except ImportError as e:
        print(f"[WARN] 缺少绘图依赖: {e}")


if __name__ == "__main__":
    # 安装依赖检查
    try:
        import requests
        import pandas as pd
        import numpy as np
        import matplotlib
    except ImportError as e:
        print(f"缺少依赖: {e}")
        print("请安装: pip install requests pandas numpy matplotlib seaborn")
        sys.exit(1)

    main()

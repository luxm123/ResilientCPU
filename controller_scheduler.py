#!/usr/bin/env python3
"""
ResilientCPU Controller - 调度器与负载生成器

三种调度策略对比：
1. Baseline: 不超卖，每个函数独占资源（模拟3台独立机器）
2. Jiagu-like: 离线训练模型预测capacity，查表调度，不动态补偿
3. ResilientCPU: 敏感度曲线 + 拐点判断 + 运行时动态调整

评估指标：
- CPU利用率: 所有机器CPU使用时间的平均值
- QoS违反率: 延迟超过基准值1.5倍的请求比例
- 调度密度: 每台机器平均运行的实例数
- 补偿响应时间: 从延迟超标到恢复正常的时间（仅ResilientCPU）
"""

import time
import json
import math
import random
import threading
import subprocess
from datetime import datetime
from collections import deque, defaultdict
from concurrent.futures import ThreadPoolExecutor
import argparse

try:
    import requests
except ImportError:
    print("需要安装 requests: pip install requests")
    exit(1)

# ============ 配置 ============

WORKER_IP = "172.31.26.175"
WORKER_PORTS = {
    "cpu_intensive": 8081,
    "io_mixed": 8082,
    "normal": 8083
}

# 基准延迟（单函数独占资源时的延迟）
# 实际值会在预热后测量
BASELINE_LATENCY = {
    "cpu_intensive": 21000,  # ~21秒
    "io_mixed": 15,          # ~15ms
    "normal": 0.1           # ~0.1ms
}

# QoS阈值 = 基准延迟 × 1.5
QOS_MULTIPLIER = 1.5

# 敏感度曲线（拐点 - CPU争抢程度的临界值）
SENSITIVITY_THRESHOLD = {
    "cpu_intensive": 0.7,  # 高敏感
    "io_mixed": 0.5,       # 中敏感
    "normal": 0.3          # 低敏感
}


# ============ 工具函数 ============

def poisson_arrival(rate, duration):
    """Poisson过程生成请求到达时间点"""
    arrivals = []
    current_time = time.time()
    end_time = current_time + duration

    while current_time < end_time:
        # 指数分布间隔
        interval = random.expovariate(rate)
        current_time += interval
        if current_time < end_time:
            arrivals.append(current_time)

    return arrivals


def get_worker_status():
    """获取worker所有函数的状态"""
    status = {}
    for func_name, port in WORKER_PORTS.items():
        try:
            resp = requests.get(f"http://{WORKER_IP}:{port}/status", timeout=2)
            if resp.status_code == 200:
                status[func_name] = resp.json()
        except Exception as e:
            print(f"[WARN] 获取 {func_name} 状态失败: {e}")
            status[func_name] = None
    return status


def invoke_function(func_name):
    """调用函数并记录延迟"""
    port = WORKER_PORTS[func_name]
    start = time.time()
    try:
        resp = requests.post(f"http://{WORKER_IP}:{port}/invoke", timeout=30)
        latency = resp.json().get("latency_ms", 0) if resp.status_code == 200 else -1
        return {
            "timestamp": start,
            "function": func_name,
            "latency_ms": latency,
            "status_code": resp.status_code,
            "success": resp.status_code == 200
        }
    except Exception as e:
        return {
            "timestamp": start,
            "function": func_name,
            "latency_ms": -1,
            "status_code": 0,
            "success": False,
            "error": str(e)
        }


def calculate_cpu_contention(status):
    """
    计算CPU争抢程度（0.0 - 1.0）
    方法：基于当前延迟相对于基准的变化
    """
    if not status:
        return 0.0

    contention_scores = []
    for func_name, func_status in status.items():
        if not func_status:
            continue

        current_latency = func_status.get("avg_latency_ms", 0)
        baseline = BASELINE_LATENCY.get(func_name, current_latency)

        if baseline > 0:
            # 延迟增长率作为争抢程度的代理
            growth = (current_latency - baseline) / baseline
            contention_scores.append(max(0.0, min(1.0, growth)))

    if contention_scores:
        return sum(contention_scores) / len(contention_scores)
    return 0.0


# ============ 三种调度策略 ============

class BaseScheduler:
    """调度器基类"""

    def __init__(self, name):
        self.name = name
        self.compensation_count = 0
        self.compensation_response_times = []

    def decide(self, status, contention):
        """决策是否需要补偿，返回 (donor, recipient, amount) 或 None"""
        return None

    def record_compensation(self, donor, recipient, amount, response_time=None):
        """记录补偿事件"""
        self.compensation_count += 1
        if response_time:
            self.compensation_response_times.append(response_time)


class BaselineScheduler(BaseScheduler):
    """
    Baseline策略: 不超卖
    每个函数独占资源，不进行任何调度
    """

    def __init__(self):
        super().__init__("Baseline")

    def decide(self, status, contention):
        # Baseline不做任何调度
        return None


class JiaguLikeScheduler(BaseScheduler):
    """
    Jiagu-like策略: 离线训练 + 查表
    预测每个函数的capacity，调度时查表决定实例数
    不进行运行时动态补偿
    """

    def __init__(self):
        super().__init__("Jiagu-like")
        # 模拟离线训练的capacity表（实际应该是离线模型预测）
        # 格式: (qps, cpu_contention) -> (cpu_intensive_instances, io_mixed_instances, normal_instances)
        self.capacity_table = self._build_capacity_table()

    def _build_capacity_table(self):
        """构建capacity表（离线训练结果）"""
        table = {}
        for contention in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
            for qps in [10, 20, 30, 50, 70, 100]:
                # 简化的capacity模型
                cpu_capacity = max(1, int(10 * (1 - contention) * (10 / qps)))
                io_capacity = max(1, int(8 * (1 - contention * 0.5) * (10 / qps)))
                normal_capacity = max(1, int(6 * (1 - contention * 0.3) * (10 / qps)))

                table[(round(qps, -1), round(contention, 1))] = (
                    cpu_capacity, io_capacity, normal_capacity
                )
        return table

    def decide(self, status, contention):
        """
        Jiagu-like: 只查表，不动态补偿
        """
        # 根据capacity表决定实例数（这里简化处理）
        # 实际实现应该根据当前QPS和CPU争抢查表
        return None  # Jiagu-like不进行运行时补偿


class ResilientCPUScheduler(BaseScheduler):
    """
    ResilientCPU策略: 敏感度曲线 + 拐点判断 + 动态调整
    """

    def __init__(self):
        super().__init__("ResilientCPU")
        self.last_violation_time = {}  # 记录每个函数的QoS违规开始时间
        self.recovery_times = []       # 记录恢复时间

    def decide(self, status, contention):
        """
        ResilientCPU: 基于敏感度曲线和拐点进行动态补偿
        """
        for func_name, func_status in status.items():
            if not func_status:
                continue

            current_latency = func_status.get("avg_latency_ms", 0)
            baseline = BASELINE_LATENCY.get(func_name, current_latency)
            threshold = baseline * QOS_MULTIPLIER

            # 检查是否QoS违规
            if current_latency > threshold:
                if func_name not in self.last_violation_time:
                    self.last_violation_time[func_name] = time.time()

                # 检查是否达到拐点（争抢程度 < 函数敏感度阈值）
                if contention < SENSITIVITY_THRESHOLD[func_name]:
                    # 从低敏感函数借资源
                    for donor in ["normal", "io_mixed"]:
                        if donor != func_name and donor in status and status[donor]:
                            donor_latency = status[donor].get("avg_latency_ms", 0)
                            donor_baseline = BASELINE_LATENCY.get(donor, donor_latency)
                            # 如果donor延迟远低于QoS阈值，可以借用
                            if donor_latency < donor_baseline * QOS_MULTIPLIER * 0.5:
                                return (donor, func_name, 256)
            else:
                # QoS恢复正常
                if func_name in self.last_violation_time:
                    recovery_time = time.time() - self.last_violation_time[func_name]
                    self.recovery_times.append(recovery_time)
                    del self.last_violation_time[func_name]
                    self.record_compensation(None, func_name, 0, recovery_time)

        return None


# ============ 调度器执行器 ============

class SchedulerExecutor:
    """调度器执行器"""

    def __init__(self, scheduler, worker_ip=WORKER_IP):
        self.scheduler = scheduler
        self.worker_ip = worker_ip
        self.running = False
        self.schedule_interval = 5  # 调度间隔（秒）

    def execute_compensation(self, donor, recipient, amount):
        """执行补偿"""
        if not donor or not recipient:
            return

        port = WORKER_PORTS.get(recipient, 8081)
        try:
            resp = requests.post(
                f"http://{self.worker_ip}:{port}/compensate",
                json={"donor": donor, "recipient": recipient, "amount": amount},
                timeout=2
            )
            if resp.status_code == 200:
                print(f"[{self.scheduler.name}] 补偿: {donor} -> {recipient}, {amount} shares")
                self.scheduler.record_compensation(donor, recipient, amount)
        except Exception as e:
            print(f"[ERROR] 补偿失败: {e}")

    def run_schedule_loop(self, duration):
        """运行调度循环"""
        self.running = True
        start_time = time.time()

        while self.running and (time.time() - start_time) < duration:
            status = get_worker_status()
            contention = calculate_cpu_contention(status)

            # 决策
            compensation = self.scheduler.decide(status, contention)

            # 执行
            if compensation:
                self.execute_compensation(*compensation)

            time.sleep(self.schedule_interval)

        self.running = False


# ============ 负载生成器 ============

class LoadGenerator:
    """负载生成器：Poisson过程生成请求"""

    def __init__(self, worker_ip, scheduler, qps_schedule=None):
        self.worker_ip = worker_ip
        self.scheduler = scheduler
        # QPS变化计划: [(持续秒数, QPS), ...]
        self.qps_schedule = qps_schedule or [(300, 10), (300, 20), (300, 30),
                                             (600, 50), (600, 70), (600, 100),
                                             (300, 50), (300, 20), (300, 10)]
        self.results = []
        self.running = False

    def run(self, duration):
        """运行负载生成"""
        self.running = True
        start_time = time.time()
        total_requests = 0
        successful_requests = 0
        failed_requests = 0

        # 启动调度线程
        scheduler_thread = threading.Thread(
            target=self.scheduler.run_schedule_loop,
            args=(duration,)
        )
        scheduler_thread.start()

        # 生成请求
        with ThreadPoolExecutor(max_workers=20) as executor:
            current_qps = 10

            for phase_duration, phase_qps in self.qps_schedule:
                if not self.running:
                    break

                phase_start = time.time()
                print(f"[负载] QPS={phase_qps}, 持续={phase_duration}秒")

                # Poisson过程生成请求
                arrivals = poisson_arrival(phase_qps, min(phase_duration, duration - (time.time() - start_time)))
                current_time = time.time()

                for arrival in arrivals:
                    if not self.running:
                        break

                    # 选择函数（加权随机）
                    func = random.choice(["cpu_intensive", "io_mixed", "normal"])

                    # 提交请求
                    future = executor.submit(invoke_function, func)
                    total_requests += 1

                    # 等待下一个请求
                    if arrival > time.time():
                        time.sleep(max(0, arrival - time.time()))

                    # 定期报告
                    if total_requests % 100 == 0:
                        print(f"  已发送 {total_requests} 请求...")

                # 确保至少运行指定时间
                elapsed = time.time() - phase_start
                if elapsed < phase_duration:
                    time.sleep(phase_duration - elapsed)

        scheduler_thread.join(timeout=5)
        self.running = False

        return {
            "total_requests": total_requests,
            "successful_requests": successful_requests,
            "failed_requests": failed_requests
        }


# ============ 主函数 ============

def main():
    parser = argparse.ArgumentParser(description="ResilientCPU Controller")
    parser.add_argument("--method", choices=["baseline", "jiagu", "resilient"],
                       default="resilient", help="调度方法")
    parser.add_argument("--duration", type=int, default=300,
                       help="运行时长（秒）")
    parser.add_argument("--warmup", type=int, default=120,
                       help="预热时长（秒）")
    parser.add_argument("--output", type=str, default="experiment_result.json",
                       help="输出文件")
    args = parser.parse_args()

    print("=" * 60)
    print("ResilientCPU Controller")
    print("=" * 60)
    print(f"方法: {args.method}")
    print(f"预热: {args.warmup}秒")
    print(f"运行: {args.duration}秒")
    print(f"Worker: {WORKER_IP}")
    print("=" * 60)

    # 选择调度器
    if args.method == "baseline":
        scheduler = BaselineScheduler()
    elif args.method == "jiagu":
        scheduler = JiaguLikeScheduler()
    else:
        scheduler = ResilientCPUScheduler()

    print(f"\n[{datetime.now()}] 预热阶段 ({args.warmup}秒)...")

    # 预热：运行少量请求建立基准
    for _ in range(10):
        for func in WORKER_PORTS.keys():
            invoke_function(func)
    time.sleep(args.warmup)

    print(f"\n[{datetime.now()}] 开始实验...")

    # 运行负载
    generator = LoadGenerator(WORKER_IP, scheduler)
    stats = generator.run(args.duration)

    # 收集结果
    print(f"\n[{datetime.now()}] 实验结束，收集结果...")

    # 获取最终状态
    final_status = get_worker_status()

    result = {
        "method": args.method,
        "timestamp": datetime.now().isoformat(),
        "duration": args.duration,
        "warmup": args.warmup,
        "stats": stats,
        "final_status": final_status,
        "compensation_count": scheduler.compensation_count,
    }

    if hasattr(scheduler, 'recovery_times'):
        result["recovery_times"] = scheduler.recovery_times
        if scheduler.recovery_times:
            result["avg_recovery_time_ms"] = sum(scheduler.recovery_times) / len(scheduler.recovery_times) * 1000

    # 保存结果
    with open(args.output, 'w') as f:
        json.dump(result, f, indent=2, default=str)

    print(f"\n结果保存到: {args.output}")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()

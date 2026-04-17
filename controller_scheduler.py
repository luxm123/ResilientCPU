#!/usr/bin/env python3
"""
ResilientCPU Controller - 调度器与负载生成器

功能：
1. 调度逻辑：读取敏感度曲线，判断worker CPU争抢程度是否小于函数拐点
2. 负载生成器：Poisson过程生成请求，QPS从10到100变化
3. 记录每个请求的延迟
"""

import time
import json
import math
import random
import requests
import threading
from datetime import datetime
from collections import deque
import argparse

# 敏感度曲线配置（拐点）
SENSITIVITY_CURVE = {
    "cpu_intensive": {
        "threshold": 0.7,      # 拐点：CPU争抢超过70%时触发保护
        "baseline_shares": 1024,
        "compensation_factor": 1.5  # 补偿系数
    },
    "io_mixed": {
        "threshold": 0.5,
        "baseline_shares": 1024,
        "compensation_factor": 1.2
    },
    "normal": {
        "threshold": 0.3,
        "baseline_shares": 1024,
        "compensation_factor": 0.8  # 可以作为donor
    }
}

# Worker配置
WORKER_IP = "172.31.26.175"
WORKER_BASE_URL = f"http://{WORKER_IP}"


class Scheduler:
    """调度器：根据敏感度曲线和CPU争抢程度进行调度"""

    def __init__(self):
        self.latency_history = {
            "cpu_intensive": deque(maxlen=100),
            "io_mixed": deque(maxlen=100),
            "normal": deque(maxlen=100)
        }
        self.qos_thresholds = {
            "cpu_intensive": 100,  # ms
            "io_mixed": 200,
            "normal": 300
        }
        self.lock = threading.Lock()

    def get_worker_status(self):
        """获取worker所有函数的状态"""
        status = {}
        for func_name in ["cpu_intensive", "io_mixed", "normal"]:
            port = {"cpu_intensive": 8081, "io_mixed": 8082, "normal": 8083}[func_name]
            try:
                resp = requests.get(f"{WORKER_BASE_URL}:{port}/status", timeout=2)
                if resp.status_code == 200:
                    status[func_name] = resp.json()
                else:
                    status[func_name] = None
            except Exception as e:
                print(f"[ERROR] Failed to get status for {func_name}: {e}")
                status[func_name] = None
        return status

    def calculate_cpu_contention(self, status):
        """
        计算CPU争抢程度
        返回：0.0 - 1.0 之间的值
        """
        # 方法：基于延迟增长率和CPU shares使用情况
        contention_scores = []

        for func_name, func_status in status.items():
            if func_status is None:
                continue

            # 获取当前延迟
            current_latency = func_status.get("avg_latency_ms", 0)
            sensitivity = func_status.get("sensitivity", 0.5)

            with self.lock:
                self.latency_history[func_name].append(current_latency)

            # 如果有历史数据，计算延迟增长率
            if len(self.latency_history[func_name]) >= 10:
                recent = list(self.latency_history[func_name])[-10:]
                old_avg = sum(recent[:5]) / 5
                new_avg = sum(recent[5:]) / 5

                if old_avg > 0:
                    growth_rate = (new_avg - old_avg) / old_avg
                    # 延迟增长率越高，争抢越严重
                    score = min(1.0, max(0.0, growth_rate * sensitivity))
                    contention_scores.append(score)

        if contention_scores:
            return sum(contention_scores) / len(contention_scores)
        return 0.0

    def should_trigger_compensation(self, func_name, contention_level):
        """
        判断是否需要触发补偿
        条件：争抢程度 < 拐点阈值 且 高敏感函数延迟超标
        """
        threshold = SENSITIVITY_CURVE[func_name]["threshold"]

        # 如果争抢程度低于拐点，说明有资源可以借用
        if contention_level < threshold:
            return True
        return False

    def decide_compensation(self, status):
        """
        决定补偿策略：从低敏感函数借CPU shares给高敏感函数
        返回：[(donor, recipient, amount), ...]
        """
        compensations = []

        # 排序函数：敏感度从高到低
        functions_ranked = sorted(
            SENSITIVITY_CURVE.keys(),
            key=lambda x: SENSITIVITY_CURVE[x]["threshold"],
            reverse=True  # 高阈值 = 高敏感
        )

        # 检查每个高敏感函数是否需要补偿
        for i, recipient in enumerate(functions_ranked[:2]):  # 检查前两个高敏感
            if status[recipient] is None:
                continue

            recipient_port = {"cpu_intensive": 8081, "io_mixed": 8082, "normal": 8083}[recipient]
            recipient_latency = status[recipient].get("avg_latency_ms", 0)
            qos_threshold = self.qos_thresholds[recipient]

            # 如果延迟超过QoS阈值，需要补偿
            if recipient_latency > qos_threshold:
                # 从低敏感函数寻找donor
                for donor in reversed(functions_ranked):  # 从低敏感开始
                    if donor == recipient:
                        continue

                    if status[donor] is None:
                        continue

                    donor_port = {"cpu_intensive": 8081, "io_mixed": 8082, "normal": 8083}[donor]
                    donor_latency = status[donor].get("avg_latency_ms", 999999)

                    # donor的延迟远低于其QoS阈值，说明有富余资源
                    donor_threshold = self.qos_thresholds[donor]
                    if donor_latency < donor_threshold * 0.5:  # 延迟低于阈值50%
                        amount = 256  # 每次借256 shares
                        compensations.append((donor, recipient, amount))
                        break

        return compensations

    def execute_compensation(self, compensations):
        """执行补偿操作"""
        results = []
        for donor, recipient, amount in compensations:
            recipient_port = {"cpu_intensive": 8081, "io_mixed": 8082, "normal": 8083}[recipient]
            try:
                resp = requests.post(
                    f"{WORKER_BASE_URL}:{recipient_port}/compensate",
                    json={"donor": donor, "amount": amount},
                    timeout=2
                )
                results.append({
                    "donor": donor,
                    "recipient": recipient,
                    "amount": amount,
                    "success": resp.status_code == 200,
                    "response": resp.json() if resp.status_code == 200 else None
                })
            except Exception as e:
                results.append({
                    "donor": donor,
                    "recipient": recipient,
                    "amount": amount,
                    "success": False,
                    "error": str(e)
                })
        return results

    def schedule_cycle(self):
        """一个调度周期"""
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ========== 调度周期开始 ==========")

        # 1. 获取worker状态
        status = self.get_worker_status()

        # 2. 计算CPU争抢程度
        contention = self.calculate_cpu_contention(status)
        print(f"CPU争抢程度: {contention:.2%}")

        # 3. 显示各函数状态
        for func_name, func_status in status.items():
            if func_status:
                print(f"  {func_name}: latency={func_status.get('avg_latency_ms', 0):.1f}ms, "
                      f"shares={func_status.get('cpu_shares', 1024)}")
            else:
                print(f"  {func_name}: OFFLINE")

        # 4. 决策是否需要补偿
        compensations = self.decide_compensation(status)
        if compensations:
            print(f"触发补偿: {compensations}")
            results = self.execute_compensation(compensations)
            for r in results:
                if r["success"]:
                    print(f"  ✓ {r['donor']} -> {r['recipient']} : {r['amount']} shares")
                else:
                    print(f"  ✗ {r['donor']} -> {r['recipient']} : 失败")
        else:
            print("无补偿需求")

        print(f"[{datetime.now().strftime('%H:%M:%S')}] ========== 调度周期结束 ==========")

        return {
            "timestamp": time.time(),
            "contention_level": contention,
            "status": status,
            "compensations": compensations
        }


def poisson_request_generator(qps, duration_seconds):
    """
    Poisson过程生成请求
    qps: 平均请求率
    duration_seconds: 持续时间
    """
    interval = 1.0 / qps  # 平均间隔
    start_time = time.time()
    request_count = 0

    while time.time() - start_time < duration_seconds:
        # 指数分布采样
        next_request_time = random.expovariate(qps)

        time.sleep(next_request_time)

        # 发送请求到随机函数
        func_choice = random.choice(["cpu_intensive", "io_mixed", "normal"])
        port = {"cpu_intensive": 8081, "io_mixed": 8082, "normal": 8083}[func_choice]

        try:
            resp = requests.post(
                f"{WORKER_BASE_URL}:{port}/invoke",
                json={},
                timeout=10
            )
            latency = resp.json().get("latency_ms", 0) if resp.status_code == 200 else -1
            yield {
                "timestamp": time.time(),
                "function": func_choice,
                "latency_ms": latency,
                "status_code": resp.status_code
            }
            request_count += 1
        except Exception as e:
            yield {
                "timestamp": time.time(),
                "function": func_choice,
                "latency_ms": -1,
                "status_code": 0,
                "error": str(e)
            }
            request_count += 1


def load_test(qps, duration_seconds, scheduler, log_file):
    """负载测试主循环"""
    print(f"\n开始负载测试: QPS={qps}, 持续时间={duration_seconds}秒")

    stats = {
        "total_requests": 0,
        "successful_requests": 0,
        "failed_requests": 0,
        "latencies": [],
        "by_function": {
            "cpu_intensive": [],
            "io_mixed": [],
            "normal": []
        }
    }

    generator = poisson_request_generator(qps, duration_seconds)

    for req in generator:
        stats["total_requests"] += 1

        if req["status_code"] == 200:
            stats["successful_requests"] += 1
            stats["latencies"].append(req["latency_ms"])
            stats["by_function"][req["function"]].append(req["latency_ms"])
        else:
            stats["failed_requests"] += 1

        # 每100个请求输出一次统计
        if stats["total_requests"] % 100 == 0:
            print(f"  已发送 {stats['total_requests']} 请求, "
                  f"成功率: {stats['successful_requests']/stats['total_requests']*100:.1f}%")

        # 每5秒执行一次调度
        if stats["total_requests"] % (qps * 5) == 0:
            scheduler.schedule_cycle()

    # 保存日志
    with open(log_file, 'a') as f:
        f.write(json.dumps({
            "timestamp": time.time(),
            "qps": qps,
            "duration": duration_seconds,
            "stats": stats
        }) + "\n")

    return stats


def main():
    parser = argparse.ArgumentParser(description="ResilientCPU Controller - 调度器与负载生成器")
    parser.add_argument("--qps", type=int, default=50, help="目标QPS")
    parser.add_argument("--duration", type=int, default=3600, help="测试持续时间（秒）")
    parser.add_argument("--log", type=str, default="controller_log.jsonl", help="日志文件")
    parser.add_argument("--schedule_interval", type=float, default=5.0, help="调度间隔（秒）")
    args = parser.parse_args()

    print(f"ResilientCPU Controller 启动")
    print(f"  Worker地址: {WORKER_BASE_URL}")
    print(f"  目标QPS: {args.qps}")
    print(f"  持续时间: {args.duration}秒")
    print(f"  日志文件: {args.log}")

    # 初始化调度器
    scheduler = Scheduler()

    # 启动调度线程
    def scheduler_loop():
        while True:
            scheduler.schedule_cycle()
            time.sleep(args.schedule_interval)

    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()

    # 等待一下确保worker服务启动
    time.sleep(3)

    # 执行负载测试
    stats = load_test(args.qps, args.duration, scheduler, args.log)

    # 输出总结
    print("\n========== 测试总结 ==========")
    print(f"总请求数: {stats['total_requests']}")
    print(f"成功率: {stats['successful_requests']/stats['total_requests']*100:.2f}%")
    if stats['latencies']:
        print(f"平均延迟: {sum(stats['latencies'])/len(stats['latencies']):.2f}ms")
        print(f"P99延迟: {sorted(stats['latencies'])[int(len(stats['latencies'])*0.99)]:.2f}ms")

    print("\n各函数统计:")
    for func, latencies in stats["by_function"].items():
        if latencies:
            print(f"  {func}: 请求数={len(latencies)}, 平均延迟={sum(latencies)/len(latencies):.1f}ms")


if __name__ == "__main__":
    main()

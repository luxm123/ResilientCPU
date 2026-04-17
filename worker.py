#!/usr/bin/env python3
"""
ResilientCPU Worker Node
支持动态CPU shares调整（通过cgroups或模拟）
提供3个函数服务，暴露状态和补偿接口
"""

import time
import math
import threading
import os
from flask import Flask, request, jsonify
from functools import wraps
from collections import deque

# 创建三个Flask应用
app_cpu = Flask("cpu_intensive")
app_io = Flask("io_mixed")
app_normal = Flask("normal")

# 全局状态
class WorkerState:
    def __init__(self, baseline_shares=1024):
        self.baseline_shares = baseline_shares
        self.cpu_shares = {
            "cpu_intensive": baseline_shares,
            "io_mixed": baseline_shares,
            "normal": baseline_shares
        }
        self.latency_history = {
            "cpu_intensive": deque(maxlen=1000),
            "io_mixed": deque(maxlen=1000),
            "normal": deque(maxlen=1000)
        }
        self.baseline_latency = {
            "cpu_intensive": None,  # 基准延迟（预热后测得）
            "io_mixed": None,
            "normal": None
        }
        self.compensation_log = []  # 记录补偿事件（用于计算响应时间）
        self.lock = threading.Lock()

        # 敏感度曲线（拐点阈值）
        self.sensitivity_threshold = {
            "cpu_intensive": 0.7,   # 高敏感
            "io_mixed": 0.5,        # 中敏感
            "normal": 0.3           # 低敏感
        }

        # QoS阈值（基准延迟的1.5倍）
        self.qos_multiplier = 1.5

state = WorkerState()


def measure_latency(func_name):
    """装饰器：测量延迟并记录"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            result = func(*args, **kwargs)
            latency = (time.perf_counter() - start) * 1000  # ms

            with state.lock:
                state.latency_history[func_name].append(latency)

            return result
        return wrapper
    return decorator


def adjust_cpu_shares_cgroup(pid, shares):
    """
    实际调整进程的CPU shares（通过cgroups v1或v2）
    这里提供实现，但需要root权限
    """
    try:
        # 方法1: cgroups v1
        cgroup_path = f"/sys/fs/cgroup/cpu/{pid}/cpu.shares"
        if os.path.exists(cgroup_path):
            with open(cgroup_path, 'w') as f:
                f.write(str(shares))
            return True

        # 方法2: cgroups v2
        cgroup2_path = f"/sys/fs/cgroup/cpu/cpu.shares"
        if os.path.exists(cgroup2_path):
            # cgroups v2 使用权重，需要转换
            # shares 1024 -> weight 10000 (大致比例)
            weight = int(shares / 1024 * 10000)
            with open(cgroup2_path, 'w') as f:
                f.write(str(weight))
            return True

        return False
    except Exception as e:
        print(f"[WARN] 无法调整cgroups（需要root）: {e}")
        return False


# ============ CPU-Intensive 服务 ============

@app_cpu.route('/invoke', methods=['POST'])
@measure_latency("cpu_intensive")
def cpu_invoke():
    """计算圆周率5000万次（持续高CPU）"""
    start = time.perf_counter()

    pi = 0.0
    iterations = 50000000
    for i in range(iterations):
        pi += 4 * ((-1) ** i) / (2 * i + 1)

    latency = (time.perf_counter() - start) * 1000
    return jsonify({
        "status": "ok",
        "latency_ms": round(latency, 2),
        "result": str(pi)[:8]
    })


@app_cpu.route('/status', methods=['GET'])
def cpu_status():
    """获取实时统计"""
    with state.lock:
        latencies = list(state.latency_history["cpu_intensive"])
        shares = state.cpu_shares["cpu_intensive"]
        baseline = state.baseline_latency["cpu_intensive"]

    if latencies:
        avg = sum(latencies) / len(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) > 20 else max(latencies)
    else:
        avg = p95 = 0

    qos_threshold = baseline * state.qos_multiplier if baseline else 999999

    return jsonify({
        "function": "cpu_intensive",
        "port": 8081,
        "sensitivity": state.sensitivity_threshold["cpu_intensive"],
        "cpu_shares": shares,
        "baseline_latency_ms": round(baseline, 2) if baseline else None,
        "avg_latency_ms": round(avg, 2),
        "p95_latency_ms": round(p95, 2),
        "qos_threshold_ms": round(qos_threshold, 2),
        "request_count": len(latencies)
    })


# ============ IO-Mixed 服务 ============

@app_io.route('/invoke', methods=['POST'])
@measure_latency("io_mixed")
def io_invoke():
    """sleep 10ms + 计算5ms（IO混合）"""
    start = time.perf_counter()

    # IO等待部分
    time.sleep(0.01)  # 10ms

    # 计算部分
    a, b = 0, 1
    for _ in range(25):  # 约5ms计算
        a, b = b, a + b

    latency = (time.perf_counter() - start) * 1000
    return jsonify({
        "status": "ok",
        "latency_ms": round(latency, 2)
    })


@app_io.route('/status', methods=['GET'])
def io_status():
    """获取实时统计"""
    with state.lock:
        latencies = list(state.latency_history["io_mixed"])
        shares = state.cpu_shares["io_mixed"]
        baseline = state.baseline_latency["io_mixed"]

    if latencies:
        avg = sum(latencies) / len(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) > 20 else max(latencies)
    else:
        avg = p95 = 0

    qos_threshold = baseline * state.qos_multiplier if baseline else 999999

    return jsonify({
        "function": "io_mixed",
        "port": 8082,
        "sensitivity": state.sensitivity_threshold["io_mixed"],
        "cpu_shares": shares,
        "baseline_latency_ms": round(baseline, 2) if baseline else None,
        "avg_latency_ms": round(avg, 2),
        "p95_latency_ms": round(p95, 2),
        "qos_threshold_ms": round(qos_threshold, 2),
        "request_count": len(latencies)
    })


# ============ Normal 服务 ============

@app_normal.route('/invoke', methods=['POST'])
@measure_latency("normal")
def normal_invoke():
    """斐波那契数列（中等负载）"""
    start = time.perf_counter()

    a, b = 0, 1
    for _ in range(30):
        a, b = b, a + b

    latency = (time.perf_counter() - start) * 1000
    return jsonify({
        "status": "ok",
        "latency_ms": round(latency, 2),
        "result": a
    })


@app_normal.route('/status', methods=['GET'])
def normal_status():
    """获取实时统计"""
    with state.lock:
        latencies = list(state.latency_history["normal"])
        shares = state.cpu_shares["normal"]
        baseline = state.baseline_latency["normal"]

    if latencies:
        avg = sum(latencies) / len(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) > 20 else max(latencies)
    else:
        avg = p95 = 0

    qos_threshold = baseline * state.qos_multiplier if baseline else 999999

    return jsonify({
        "function": "normal",
        "port": 8083,
        "sensitivity": state.sensitivity_threshold["normal"],
        "cpu_shares": shares,
        "baseline_latency_ms": round(baseline, 2) if baseline else None,
        "avg_latency_ms": round(avg, 2),
        "p95_latency_ms": round(p95, 2),
        "qos_threshold_ms": round(qos_threshold, 2),
        "request_count": len(latencies)
    })


# ============ 补偿接口 ============

@app_cpu.route('/compensate', methods=['POST'])
@app_io.route('/compensate', methods=['POST'])
@app_normal.route('/compensate', methods=['POST'])
def compensate():
    """
    调整CPU shares（从低敏感函数借给高敏感函数）
    请求体: {"donor": "normal", "recipient": "cpu_intensive", "amount": 256}
    """
    data = request.get_json() or {}
    donor = data.get('donor', 'normal')
    recipient = data.get('recipient', 'cpu_intensive')
    amount = data.get('amount', 256)

    with state.lock:
        # 验证
        if donor not in state.cpu_shares or recipient not in state.cpu_shares:
            return jsonify({"status": "error", "message": "Invalid function name"}), 400

        if donor == recipient:
            return jsonify({"status": "error", "message": "Donor and recipient cannot be the same"}), 400

        if state.cpu_shares[donor] < amount:
            return jsonify({
                "status": "error",
                "message": f"{donor} insufficient shares (have: {state.cpu_shares[donor]}, need: {amount})"
            }), 400

        # 执行补偿
        old_donor = state.cpu_shares[donor]
        old_recipient = state.cpu_shares[recipient]

        state.cpu_shares[donor] -= amount
        state.cpu_shares[recipient] += amount

        # 记录补偿事件（用于计算响应时间）
        state.compensation_log.append({
            "timestamp": time.time(),
            "donor": donor,
            "recipient": recipient,
            "amount": amount,
            "donor_shares_after": state.cpu_shares[donor],
            "recipient_shares_after": state.cpu_shares[recipient]
        })

        # 尝试应用cgroups调整（如果权限足够）
        # 这里简化：只更新状态，实际cgroup调整需要root
        # adjust_cpu_shares_cgroup(os.getpid(), state.cpu_shares[recipient])

        return jsonify({
            "status": "ok",
            "donor": donor,
            "recipient": recipient,
            "amount": amount,
            "donor_shares": state.cpu_shares[donor],
            "recipient_shares": state.cpu_shares[recipient],
            "compensation_time": time.time()
        })


@app_cpu.route('/reset', methods=['POST'])
@app_io.route('/reset', methods=['POST'])
@app_normal.route('/reset', methods=['POST'])
def reset_shares():
    """重置为Baseline shares（用于实验）"""
    with state.lock:
        for func in state.cpu_shares:
            state.cpu_shares[func] = state.baseline_shares
        state.compensation_log.clear()
        state.latency_history.clear()
        for func in state.latency_history:
            state.latency_history[func] = deque(maxlen=1000)

    return jsonify({"status": "ok", "message": "CPU shares reset to baseline"})


@app_cpu.route('/set_baseline', methods=['POST'])
@app_io.route('/set_baseline', methods=['POST'])
@app_normal.route('/set_baseline', methods=['POST'])
def set_baseline():
    """设置基准延迟（预热后调用）"""
    data = request.get_json() or {}
    func = data.get('function', 'cpu_intensive')
    baseline = data.get('baseline_latency_ms', 0)

    with state.lock:
        if func in state.baseline_latency:
            state.baseline_latency[func] = baseline

    return jsonify({"status": "ok", "function": func, "baseline": baseline})


# ============ 健康检查 ============

@app_cpu.route('/health', methods=['GET'])
@app_io.route('/health', methods=['GET'])
@app_normal.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy", "timestamp": time.time()})


# ============ 主函数 ============

def run_worker(host='0.0.0.0'):
    threads = []

    ports = [
        (app_cpu, 8081),
        (app_io, 8082),
        (app_normal, 8083)
    ]

    for app, port in ports:
        t = threading.Thread(
            target=app.run,
            kwargs={'host': host, 'port': port, 'debug': False, 'use_reloader': False, 'threaded': True}
        )
        t.daemon = True
        threads.append(t)
        t.start()
        print(f"  [{app.name}] listening on :{port}")

    print(f"\nWorker node started on {host}")
    print(f"Baseline CPU shares: {state.baseline_shares}")
    print(f"QoS multiplier: {state.qos_multiplier}x")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == '__main__':
    run_worker()

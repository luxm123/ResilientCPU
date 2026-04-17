#!/usr/bin/env python3
"""
ResilientCPU Worker Node
提供3个HTTP服务端口，对应不同敏感度函数
支持CPU shares补偿机制
"""

import time
import math
import threading
from flask import Flask, request, jsonify
from functools import wraps
from collections import deque

# 创建三个Flask应用，分别对应不同端口
app_cpu_intensive = Flask("cpu_intensive")
app_io_mixed = Flask("io_mixed")
app_normal = Flask("normal")

# 全局状态存储
class WorkerState:
    def __init__(self):
        self.latencies = {
            "cpu_intensive": deque(maxlen=1000),
            "io_mixed": deque(maxlen=1000),
            "normal": deque(maxlen=1000)
        }
        self.lock = threading.Lock()
        self.cpu_shares = {
            "cpu_intensive": 1024,
            "io_mixed": 1024,
            "normal": 1024
        }
        self.sensitivity_curve = {
            "cpu_intensive": 0.9,   # 高敏感
            "io_mixed": 0.5,        # 中敏感
            "normal": 0.1           # 低敏感
        }

state = WorkerState()


def measure_latency(func_name):
    """装饰器：测量函数执行延迟"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            result = func(*args, **kwargs)
            latency = (time.time() - start) * 1000  # ms

            with state.lock:
                state.latencies[func_name].append(latency)

            return result
        return wrapper
    return decorator


# ============ CPU-Intensive 服务 (端口8081) ============

@app_cpu_intensive.route('/invoke', methods=['POST'])
@measure_latency("cpu_intensive")
def cpu_intensive_invoke():
    """计算圆周率5000万次"""
    start_time = time.time()

    # 计算圆周率：使用 Leibniz 公式
    pi = 0.0
    iterations = 50000000
    for i in range(iterations):
        pi += 4 * ((-1) ** i) / (2 * i + 1)

    latency = (time.time() - start_time) * 1000
    return jsonify({
        "status": "ok",
        "latency_ms": round(latency, 2),
        "result": str(pi)[:10]  # 返回部分结果防止响应过大
    })


@app_cpu_intensive.route('/status', methods=['GET'])
def cpu_intensive_status():
    """获取实时延迟统计"""
    with state.lock:
        latencies = list(state.latencies["cpu_intensive"])

    if latencies:
        avg_latency = sum(latencies) / len(latencies)
        max_latency = max(latencies)
        p99 = sorted(latencies)[int(len(latencies) * 0.99)] if len(latencies) > 100 else max_latency
    else:
        avg_latency = max_latency = p99 = 0

    return jsonify({
        "function": "cpu_intensive",
        "port": 8081,
        "sensitivity": state.sensitivity_curve["cpu_intensive"],
        "cpu_shares": state.cpu_shares["cpu_intensive"],
        "avg_latency_ms": round(avg_latency, 2),
        "max_latency_ms": round(max_latency, 2),
        "p99_latency_ms": round(p99, 2),
        "request_count": len(latencies)
    })


# ============ IO-Mixed 服��� (端口8082) ============

@app_io_mixed.route('/invoke', methods=['POST'])
@measure_latency("io_mixed")
def io_mixed_invoke():
    """循环 sleep 10ms + 计算 5ms"""
    start_time = time.time()
    total_sleep = 0
    total_compute = 0

    # 模拟IO等待 + CPU计算混合负载
    while total_sleep < 0.01:  # 至少sleep 10ms
        time.sleep(0.001)  # sleep 1ms
        total_sleep += 0.001

        # 计算Fibonacci数列前20项（约5ms）
        n = 20
        a, b = 0, 1
        for _ in range(n):
            a, b = b, a + b
        total_compute += 0.005

    latency = (time.time() - start_time) * 1000
    return jsonify({
        "status": "ok",
        "latency_ms": round(latency, 2)
    })


@app_io_mixed.route('/status', methods=['GET'])
def io_mixed_status():
    """获取实时延迟统计"""
    with state.lock:
        latencies = list(state.latencies["io_mixed"])

    if latencies:
        avg_latency = sum(latencies) / len(latencies)
        max_latency = max(latencies)
        p99 = sorted(latencies)[int(len(latencies) * 0.99)] if len(latencies) > 100 else max_latency
    else:
        avg_latency = max_latency = p99 = 0

    return jsonify({
        "function": "io_mixed",
        "port": 8082,
        "sensitivity": state.sensitivity_curve["io_mixed"],
        "cpu_shares": state.cpu_shares["io_mixed"],
        "avg_latency_ms": round(avg_latency, 2),
        "max_latency_ms": round(max_latency, 2),
        "p99_latency_ms": round(p99, 2),
        "request_count": len(latencies)
    })


# ============ Normal 服务 (端口8083) ============

@app_normal.route('/invoke', methods=['POST'])
@measure_latency("normal")
def normal_invoke():
    """计算斐波那契数列（第30项）"""
    start_time = time.time()

    def fibonacci(n):
        if n <= 1:
            return n
        a, b = 0, 1
        for _ in range(n):
            a, b = b, a + b
        return a

    result = fibonacci(30)

    latency = (time.time() - start_time) * 1000
    return jsonify({
        "status": "ok",
        "latency_ms": round(latency, 2),
        "result": result
    })


@app_normal.route('/status', methods=['GET'])
def normal_status():
    """获取实时延迟统计"""
    with state.lock:
        latencies = list(state.latencies["normal"])

    if latencies:
        avg_latency = sum(latencies) / len(latencies)
        max_latency = max(latencies)
        p99 = sorted(latencies)[int(len(latencies) * 0.99)] if len(latencies) > 100 else max_latency
    else:
        avg_latency = max_latency = p99 = 0

    return jsonify({
        "function": "normal",
        "port": 8083,
        "sensitivity": state.sensitivity_curve["normal"],
        "cpu_shares": state.cpu_shares["normal"],
        "avg_latency_ms": round(avg_latency, 2),
        "max_latency_ms": round(max_latency, 2),
        "p99_latency_ms": round(p99, 2),
        "request_count": len(latencies)
    })


# ============ 补偿接口 (每个服务都有) ============

@app_cpu_intensive.route('/compensate', methods=['POST'])
@app_io_mixed.route('/compensate', methods=['POST'])
@app_normal.route('/compensate', methods=['POST'])
def compensate():
    """
    补偿接口：从低敏感函数借CPU shares
    请求体: {"donor": "normal"} 或 {"donor": "io_mixed"}
    """
    data = request.get_json() or {}
    donor = data.get('donor', 'normal')  # 默认从normal借
    amount = data.get('amount', 256)     # 默认借256 shares

    with state.lock:
        # 检查 donor 是否有效（必须是低敏感函数）
        if donor not in ['normal', 'io_mixed']:
            return jsonify({
                "status": "error",
                "message": f"Invalid donor function: {donor}. Can only borrow from normal or io_mixed"
            }), 400

        # 检查 donor 是否有足够 shares
        if state.cpu_shares[donor] < amount:
            return jsonify({
                "status": "error",
                "message": f"{donor} does not have enough shares (requested: {amount}, available: {state.cpu_shares[donor]})"
            }), 400

        # 执行补偿（假设是从低敏感借给高敏感，需要推断recipient）
        # 这里简化：根据请求上下文推断recipient
        recipient = request.path.split('/')[1]  # 从路径推断
        if recipient not in ['cpu_intensive', 'io_mixed', 'normal']:
            recipient = 'cpu_intensive'  # 默认补偿给高敏感

        # 执行转移
        state.cpu_shares[donor] -= amount
        state.cpu_shares[recipient] += amount

        return jsonify({
            "status": "ok",
            "message": f"Borrowed {amount} shares from {donor} to {recipient}",
            "donor": donor,
            "recipient": recipient,
            "donor_shares": state.cpu_shares[donor],
            "recipient_shares": state.cpu_shares[recipient]
        })


# ============ 健康检查 ============

@app_cpu_intensive.route('/health', methods=['GET'])
@app_io_mixed.route('/health', methods=['GET'])
@app_normal.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy", "timestamp": time.time()})


# ============ 主函数 ============

def run_worker(host='0.0.0.0', port_base=8080):
    """启动三个HTTP服务"""
    threads = []

    # 启动 cpu_intensive 服务 (8081)
    t1 = threading.Thread(
        target=app_cpu_intensive.run,
        kwargs={'host': host, 'port': 8081, 'debug': False, 'use_reloader': False}
    )
    threads.append(t1)

    # 启动 io_mixed 服务 (8082)
    t2 = threading.Thread(
        target=app_io_mixed.run,
        kwargs={'host': host, 'port': 8082, 'debug': False, 'use_reloader': False}
    )
    threads.append(t2)

    # 启动 normal 服务 (8083)
    t3 = threading.Thread(
        target=app_normal.run,
        kwargs={'host': host, 'port': 8083, 'debug': False, 'use_reloader': False}
    )
    threads.append(t3)

    for t in threads:
        t.daemon = True
        t.start()

    print(f"Worker node started on {host}")
    print(f"  - cpu_intensive service : http://{host}:8081")
    print(f"  - io_mixed service      : http://{host}:8082")
    print(f"  - normal service        : http://{host}:8083")

    # 保持主线程运行
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down worker...")


if __name__ == '__main__':
    run_worker()

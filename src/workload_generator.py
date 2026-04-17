"""
工作负载生成器 - Workload Generator

生成符合真实FaaS场景的函数调用请求：
- 使用合成工作负载（恒定到达率、突发、周期性）
- 支持多种分布（泊松、重尾、工作日/周末模式）
- 包含函数类型选择（符合真实比例）
- 可以指定干扰时间模式
"""

import random
import numpy as np
from typing import List, Optional, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass

from .types import Function, Invocation


@dataclass
class WorkloadPattern:
    """工作负载模式"""
    pattern_type: str  # "uniform" | "poisson" | "bursty" | "diurnal" | "weekly"
    base_rate: float   # 基础到达率（每秒请求数）
    burst_multiplier: float = 5.0   # 突发倍数
    diurnal_amplitude: float = 0.5  # 昼夜波动幅度


class WorkloadGenerator:
    """
    工作负载生成器
    
    生成策略：
    1. 函数选择：基于历史调用频率的幂律分布
    2. 到达间隔：符合真实场景的分布
    3. 负载变化：模拟工作日的周期性和突发流量
    """

    # Azure Functions公开数据集的工作负载比例（参考）
    AZURE_FUNCTION_POPULARITY = {
        "timer": 0.35,          # 定时触发器
        "event": 0.30,          # 事件驱动
        "http": 0.25,           # HTTP请求
        "manual": 0.10          # 手动调用
    }

    # 函数类型流行度（参考）
    FUNCTION_POPULARITY = {
        "apigateway": 0.20,
        "textparse": 0.15,
        "logprocess": 0.12,
        "datacompress": 0.10,
        "imagefilter": 0.08,
        "mlinference": 0.07,
        "dbcheck": 0.06,
        "imageresize": 0.06,
        "reportgen": 0.08,
        "mediatranscode": 0.08
    }

    def __init__(
        self,
        functions: List[Function],
        arrival_rate: float = 50.0,      # 平均每秒到达率
        duration: float = 3600.0,         # 工作负载持续时间（秒）
        pattern: WorkloadPattern = None,
        seed: int = 42
    ):
        self.functions = functions
        self.arrival_rate = arrival_rate
        self.duration = duration
        self.pattern = pattern or WorkloadPattern("poisson", arrival_rate)
        self.seed = seed
        random.seed(seed)
        np.random.seed(seed)

        # 预计算每个函数的选择概率
        self._compute_selection_probabilities()

        # 时间追踪
        self.current_time = 0.0

        # 缓存到达时间
        self.arrival_times: List[float] = []

    def _compute_selection_probabilities(self) -> None:
        """计算函数选择概率"""
        # 归一化函数选择概率
        total = sum(self.FUNCTION_POPULARITY.values())
        self.function_probs = {
            f.function_id: self.FUNCTION_POPULARITY.get(
                f.function_id.replace("_", ""), 0.01
            ) / total
            for f in self.functions
        }

    def generate(self) -> List[Invocation]:
        """
        生成完整的工作负载序列

        Returns:
            Invocation列表
        """
        print(f"[Workload] 生成工作负载: duration={self.duration}s, base_rate={self.arrival_rate}/s")

        invocations = []
        time_cursor = 0.0

        while time_cursor < self.duration:
            # 计算当前时刻的到达率（考虑模式）
            current_rate = self._get_dynamic_arrival_rate(time_cursor)

            # 生成下一个到达的间隔时间
            if self.pattern.pattern_type == "poisson":
                # 指数分布（泊松过程）
                inter_arrival = np.random.exponential(1.0 / current_rate)
            elif self.pattern.pattern_type == "uniform":
                # 均匀分布
                inter_arrival = random.expovariate(current_rate)
            elif self.pattern.pattern_type == "bursty":
                # 突发模式：在宁静期后突然出现大量请求
                burst_prob = 0.1  # 10%的概率触发突发
                if random.random() < burst_prob:
                    inter_arrival = np.random.exponential(1.0 / (current_rate * self.pattern.burst_multiplier))
                else:
                    inter_arrival = np.random.exponential(1.0 / (current_rate * 0.3))
            elif self.pattern.pattern_type == "diurnal":
                # 昼夜模式：白天负载高，夜晚低
                inter_arrival = np.random.exponential(1.0 / current_rate)
            else:
                inter_arrival = np.random.exponential(1.0 / current_rate)

            time_cursor += inter_arrival

            if time_cursor >= self.duration:
                break

            # 选择函数
            func = self._select_function()

            # 创建调用请求
            invocation = Invocation(
                invocation_id=f"inv_{len(invocations)}",
                function_id=func.function_id,
                arrival_time=time_cursor,
                deadline=time_cursor + func.timeout * 3  # 宽松的截止时间
            )

            invocations.append(invocation)

        print(f"[Workload] 生成了 {len(invocations)} 个调用请求")
        return invocations

    def _get_dynamic_arrival_rate(self, t: float) -> float:
        """根据时间获取动态到达率"""
        if self.pattern.pattern_type == "diurnal":
            # 模拟昼夜周期：假设t=0是午夜，t=86400是第二天午夜
            hour = (t % 86400) / 3600

            # 白天（8-20点）高负载，夜晚低负载
            if 8 <= hour <= 20:
                factor = 1.0 + self.pattern.diurnal_amplitude
            else:
                factor = 1.0 - self.pattern.diurnal_amplitude * 0.5

            return self.arrival_rate * factor

        elif self.pattern.pattern_type == "weekly":
            # 周模式：工作日vs周末
            day = (t // 86400) % 7
            if day < 5:  # 工作日
                return self.arrival_rate * 1.2
            else:  # 周末
                return self.arrival_rate * 0.6

        return self.arrival_rate

    def _select_function(self) -> Function:
        """基于流行度选择函数"""
        func_ids = list(self.function_probs.keys())
        probs = [self.function_probs[fid] for fid in func_ids]
        selected_id = np.random.choice(func_ids, p=probs)

        for func in self.functions:
            if func.function_id == selected_id:
                return func

        # 回退：随机选择
        return random.choice(self.functions)

    def generate_async(self) -> Invocation:
        """
        异步生成模式：逐个产生调用请求

        生成器函数，用于流式生成
        """
        time_cursor = 0.0

        while time_cursor < self.duration:
            current_rate = self._get_dynamic_arrival_rate(time_cursor)
            inter_arrival = np.random.exponential(1.0 / current_rate)
            time_cursor += inter_arrival

            if time_cursor >= self.duration:
                break

            func = self._select_function()
            invocation = Invocation(
                invocation_id=f"inv_{time.time_ns()}",
                function_id=func.function_id,
                arrival_time=time_cursor,
                deadline=time_cursor + func.timeout * 3
            )

            yield invocation


def create_azure_inspired_workload(
    num_machines: int = 4,
    avg_arrival_rate: float = 100.0
) -> WorkloadGenerator:
    """
    创建Azure公开数据集风格的负载

    特点：
    - 80%函数属于2%的热门函数（长尾分布）
    - 定时触发占35%
    - 周期性访问模式
    """
    functions = []

    # 热门函数（2%）
    hot_functions = [
        Function("api_gateway", "APIGateway", 512, 0.5, 0.05, 0.01, 1.0),
        Function("user_auth", "UserAuth", 256, 0.5, 0.15, 0.03, 3.0),
        Function("data_fetch", "DataFetch", 1024, 1.0, 0.5, 0.1, 5.0),
        Function("log_writer", "LogWriter", 256, 0.5, 0.1, 0.02, 2.0),
        Function("cache_refresh", "CacheRefresh", 512, 1.0, 0.3, 0.05, 3.0),
    ]

    # 冷门函数（98%）
    tail_functions = []
    for i in range(50):
        tail_functions.append(
            Function(
                function_id=f"tail_func_{i:03d}",
                name=f"TailFunc{i}",
                memory_mb=random.choice([256, 512, 1024]),
                cpu_cores=random.choice([0.5, 1.0, 2.0]),
                execution_time_mean=random.uniform(0.1, 5.0)
            )
        )

    functions.extend(hot_functions)
    functions.extend(tail_functions)

    # 生成负载模式
    pattern = WorkloadPattern(
        pattern_type="diurnal",
        base_rate=avg_arrival_rate,
        diurnal_amplitude=0.6
    )

    generator = WorkloadGenerator(
        functions=functions,
        arrival_rate=avg_arrival_rate,
        duration=3600,
        pattern=pattern
    )

    return generator


def create_bursty_workload(
    functions: List[Function],
    avg_rate: float = 100.0,
    burst_prob: float = 0.05
) -> WorkloadGenerator:
    """创建突发性负载（用于测试鲁棒性）"""
    pattern = WorkloadPattern(
        pattern_type="bursty",
        base_rate=avg_rate,
        burst_multiplier=10.0
    )

    return WorkloadGenerator(
        functions=functions,
        arrival_rate=avg_rate,
        duration=600,
        pattern=pattern
    )


if __name__ == "__main__":
    # 快速测试
    from .types import Function

    functions = [
        Function("f1", "Func1", 512, 1.0, 1.0, 0.2),
        Function("f2", "Func2", 1024, 2.0, 2.0, 0.3),
        Function("f3", "Func3", 256, 0.5, 0.5, 0.1),
    ]

    gen = WorkloadGenerator(functions, arrival_rate=10.0, duration=10.0)
    invocs = gen.generate()

    print(f"生成了 {len(invocs)} 个调用:")
    for inv in invocs[:10]:
        print(f"  {inv.invocation_id}: {inv.function_id} @ t={inv.arrival_time:.2f}s")

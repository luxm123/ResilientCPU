"""
ResilientCPU 核心数据结构定义
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum
import numpy as np


class FunctionState(Enum):
    """函数实例状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SCALING = "scaling"


class InterferenceLevel(Enum):
    """干扰程度等级"""
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class SensitivityProfile:
    """
    函数敏感度曲线
    描述函数在不同CPU争抢程度下的性能表现
    """
    function_id: str
    # 特征点: (cpu_contention_percentage, performance_degradation)
    # 例如: [(0, 0.0), (20, 0.05), (40, 0.15), (60, 0.35), (80, 0.70), (100, 0.95)]
    sensitivity_points: List[Tuple[float, float]]
    # 拐点: 性能开始显著下降的CPU争抢百分比
    knee_point: float
    # 可接受的性能退化阈值（如5%）
    acceptable_degradation: float = 0.05
    # 拟合的S型曲线参数
    fitted_params: Optional[Tuple[float, float, float, float]] = None  # L, x0, k, b

    def get_performance_degradation(self, contention: float) -> float:
        """
        根据CPU争抢程度预测性能退化
        使用S形曲线插值或分段线性插值
        """
        if contention <= 0:
            return 0.0
        if contention >= 100:
            return 1.0

        # 使用拟合的Logistic曲线
        if self.fitted_params is not None:
            L, x0, k, b = self.fitted_params
            # Logistic函数: L / (1 + exp(-k*(x-x0))) + b
            normalized = L / (1 + np.exp(-k * (contention - x0))) + b
            return max(0.0, min(1.0, normalized))

        # 分段线性插值
        for i in range(len(self.sensitivity_points) - 1):
            x1, y1 = self.sensitivity_points[i]
            x2, y2 = self.sensitivity_points[i + 1]

            if x1 <= contention <= x2:
                if x2 == x1:
                    return y1
                ratio = (contention - x1) / (x2 - x1)
                return y1 + ratio * (y2 - y1)

        # 超出范围
        return 1.0 if contention > self.sensitivity_points[-1][0] else 0.0

    def is_acceptable_contention(self, contention: float) -> bool:
        """给定的CPU争抢程度是否可接受"""
        degradation = self.get_performance_degradation(contention)
        return degradation <= self.acceptable_degradation

    def get_max_acceptable_contention(self) -> float:
        """获取可接受的最大CPU争抢程度"""
        # 二分查找找到临界点
        low, high = 0.0, 100.0
        while high - low > 0.1:
            mid = (low + high) / 2
            if self.is_acceptable_contention(mid):
                low = mid
            else:
                high = mid
        return low


@dataclass
class Function:
    """FaaS函数定义"""
    function_id: str
    name: str
    memory_mb: int
    cpu_cores: float  # 可以是小数（如0.5核心）
    execution_time_mean: float
    execution_time_std: float = 0.2
    timeout: float = 30.0
    image: str = "generic"
    # 敏感度曲线
    sensitivity_profile: Optional[SensitivityProfile] = None
    # 运行时状态
    state: FunctionState = FunctionState.PENDING
    # 分配的资源
    allocated_cpu_shares: int = 1024  # cgroup CPU shares (默认1024=1核)
    allocated_cpus: Optional[List[str]] = None  # 绑定的CPU核心列表

    def __post_init__(self):
        if self.allocated_cpus is None:
            self.allocated_cpus = []


@dataclass
class Invocation:
    """函数调用请求"""
    invocation_id: str
    function_id: str
    arrival_time: float
    duration: Optional[float] = None  # 实际执行时长
    deadline: Optional[float] = None   # 截止时间
    # 执行状态
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    sla_violated: bool = False
    # 性能指标
    cpu_usage: Optional[float] = None
    memory_usage: Optional[float] = None


@dataclass
class Machine:
    """物理机器/虚拟机节点"""
    machine_id: str
    cpu_cores: int
    memory_mb: int
    # 已分配CPU shares总量（用于超卖计算）
    total_cpu_shares_allocated: int = 0
    max_cpu_shares: int = 1024 * 8  # 假设每核1024 shares
    # 当前运行函数
    running_functions: Dict[str, Function] = field(default_factory=dict)
    # 实时CPU争抢程度估计（0-100%）
    cpu_contention: float = 0.0
    # 资源使用统计
    cpu_utilization: float = 0.0
    memory_utilization: float = 0.0
    # 本地调度器
    local_scheduler_enabled: bool = True

    def available_cpu_shares(self) -> int:
        """可用CPU shares"""
        return self.max_cpu_shares - self.total_cpu_shares_allocated

    def can_allocate(self, cpu_shares: int) -> bool:
        """是否可以分配指定CPU shares"""
        return self.available_cpu_shares() >= cpu_shares

    def total_allocated_cores(self) -> float:
        """已分配的CPU核心数（基于shares换算）"""
        return self.total_cpu_shares_allocated / 1024.0


@dataclass
class SchedulingDecision:
    """调度决策"""
    invocation_id: str
    function_id: str
    selected_machine: str
    scheduling_time: float
    # 决策依据
    contention_estimate: float
    sensitivity_used: bool
    predicted_degradation: float
    confidence: float = 1.0


@dataclass
class MonitoringSample:
    """监控采样"""
    timestamp: float
    machine_id: str
    function_id: Optional[str]
    # 资源使用
    cpu_usage: float
    memory_usage: float
    cpu_shares: int
    # 性能指标
    latency_ms: Optional[float] = None
    sla_target_ms: Optional[float] = None
    sla_violated: bool = False


@dataclass
class ExperimentResult:
    """实验结果"""
    experiment_id: str
    scheduler_type: str
    # 性能指标
    avg_latency: float
    p95_latency: float
    p99_latency: float
    sla_violation_rate: float
    throughput: float  # 每秒完成请求数
    # 资源利用率
    avg_cpu_utilization: float
    peak_cpu_utilization: float
    resource_efficiency: float  # 资源利用效率
    # 稳定性
    latency_cv: float  # 延迟变异系数
    # 敏感度曲线拟合误差
    profiling_error: float

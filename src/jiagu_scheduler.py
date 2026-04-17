"""
Jiagu-like 调度器 - 基于容量预测的保守调度

Jiagu原版论文思路：
1. 离线训练模型预测每台机器还能容纳多少函数实例（capacity）
2. 调度时查表，不超过capacity就放置
3. 不做动态补偿——干扰发生时不调整资源

这个实现模拟Jiagu的核心思想，但使用简单的线性回归模型
"""

import asyncio
import random
import numpy as np
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import joblib
import os

from .types import Function, Invocation, Machine, SchedulingDecision, FunctionState
from .cgroup_manager import CGroupManager


class JiaguLikeScheduler:
    """
    Jiagu-like 调度器
    
    核心特点：
    1. 离线训练容量预测模型
    2. 调度时查询模型，获得当前机器的剩余容量
    3. 只放置不超过容量的函数
    4. 运行时不做任何资源补偿
    """

    def __init__(
        self,
        machines: List[Machine],
        model_path: Optional[str] = None,
        capacity_update_interval: float = 60.0  # 容量表更新间隔（秒）
    ):
        """
        Args:
            machines: 机器列表
            model_path: 预训练模型路径（如果为None则在线训练）
            capacity_update_interval: 容量表更新间隔
        """
        self.machines = {m.machine_id: m for m in machines}
        self.functions: Dict[str, Function] = {}
        self.model_path = model_path
        self.capacity_update_interval = capacity_update_interval

        # 容量表：machine_id -> (function_type -> capacity)
        self.capacity_table: Dict[str, Dict[str, int]] = {}
        self.last_update_time: Dict[str, float] = {}

        # 模型（线性回归）
        self.capacity_model = None
        self.feature_scaler = None

        # 统计
        self.scheduling_decisions: List[SchedulingDecision] = []
        self.rejected_invocations: List[Invocation] = []

        # cgroup管理器（仅用于放置，不做动态调整）
        self.cgroup_mgr = CGroupManager()

        # 自动训练模型
        if model_path and os.path.exists(model_path):
            self._load_model(model_path)
        else:
            self._train_model()

    def _train_model(self) -> None:
        """
        训练容量预测模型
        
        特征工程：
        - 机器CPU使用率（0-1）
        - 机器当前运行的函数类型分布（one-hot或计数）
        - 函数CPU需求
        - 函数内存需求
        
        目标：
        - 还能放置的该类型函数实例数
        """
        print("[Jiagu] 训练容量预测模型...")

        # 生成训练数据（模拟）
        # 在真实场景中，这里使用历史调度数据
        X, y = self._generate_training_data(n_samples=1000)

        # 简单的线性回归
        from sklearn.linear_model import LinearRegression
        from sklearn.preprocessing import StandardScaler

        self.feature_scaler = StandardScaler()
        X_scaled = self.feature_scaler.fit_transform(X)

        self.capacity_model = LinearRegression()
        self.capacity_model.fit(X_scaled, y)

        # 评估
        score = self.capacity_model.score(X_scaled, y)
        print(f"[Jiagu] 模型训练完成，R² score: {score:.3f}")

        # 保存模型
        if self.model_path:
            joblib.dump({
                'model': self.capacity_model,
                'scaler': self.feature_scaler
            }, self.model_path)
            print(f"[Jiagu] 模型已保存到 {self.model_path}")

    def _generate_training_data(self, n_samples: int = 1000) -> Tuple[np.ndarray, np.ndarray]:
        """
        生成训练数据
        
        模拟不同负载下的容量：
        - 特征：当前CPU使用率 + 各类型函数实例数
        - 目标：还能放置的同类型函数数量
        """
        X = []
        y = []

        function_types = ["high", "medium", "low"]

        for _ in range(n_samples):
            # 随机生成机器状态
            cpu_usage = random.uniform(0.1, 0.9)

            # 随机生成已有函数分布（3种类型）
            existing_counts = [
                random.randint(0, 5),  # high敏感度函数数量
                random.randint(0, 8),  # medium
                random.randint(0, 12)  # low
            ]

            # 对于每种函数类型，计算剩余容量
            for type_idx, func_type in enumerate(function_types):
                existing = existing_counts[type_idx]

                # 模拟容量计算规则（基于资源消耗）
                # 假设每台机器8核
                total_cores = 8.0
                used_cores = cpu_usage * total_cores
                # 每个函数的CPU需求（根据类型）
                cpu_per_func = {"high": 2.0, "medium": 1.5, "low": 1.0}[func_type]

                # 剩余核心数
                remaining_cores = total_cores - used_cores
                # 剩余容量（向下取整）
                remaining_capacity = max(0, int(remaining_cores / cpu_per_func))

                # 构建特征向量
                features = [
                    cpu_usage,
                    existing_counts[0],  # high数量
                    existing_counts[1],  # medium数量
                    existing_counts[2],  # low数量
                    {"high": 2.0, "medium": 1.5, "low": 1.0}[func_type],  # CPU需求
                ]

                X.append(features)
                y.append(remaining_capacity)

        return np.array(X), np.array(y)

    def _load_model(self, model_path: str) -> None:
        """加载预训练模型"""
        try:
            data = joblib.load(model_path)
            self.capacity_model = data['model']
            self.feature_scaler = data['scaler']
            print(f"[Jiagu] 模型已从 {model_path} 加载")
        except Exception as e:
            print(f"[Jiagu] 模型加载失败: {e}，将重新训练")
            self._train_model()

    def register_function(self, function: Function) -> None:
        """注册函数"""
        self.functions[function.function_id] = function

    async def schedule(self, invocation: Invocation) -> Optional[SchedulingDecision]:
        """
        调度函数请求
        
        Jiagu调度逻辑：
        1. 定期更新容量表（每60秒）
        2. 获取目标函数类型
        3. 选择负载最低的机器
        4. 查询该机器对该类型函数的剩余容量
        5. 如果容量>0，则放置；否则拒绝
        """
        function = self.functions.get(invocation.function_id)
        if function is None:
            return None

        # 更新容量表（如果过期）
        current_time = invocation.arrival_time
        for machine_id in self.machines:
            if (machine_id not in self.last_update_time or
                current_time - self.last_update_time.get(machine_id, 0) > self.capacity_update_interval):
                self._update_machine_capacity(machine_id)
                self.last_update_time[machine_id] = current_time

        # 确定函数类型���基于敏感度）
        func_type = self._get_function_type(function)

        # 选择候选机器
        candidate_machines = self._get_candidate_machines(function)

        if not candidate_machines:
            self.rejected_invocations.append(invocation)
            return None

        # 选择剩余容量>0的机器
        for machine_id in candidate_machines:
            capacity = self.capacity_table.get(machine_id, {}).get(func_type, 0)
            if capacity > 0:
                # 可以放置
                machine = self.machines[machine_id]

                # 预留资源
                self._reserve_resources(function, machine_id)

                # 创建调度决策
                decision = SchedulingDecision(
                    invocation_id=invocation.invocation_id,
                    function_id=invocation.function_id,
                    selected_machine=machine_id,
                    scheduling_time=invocation.arrival_time,
                    contention_estimate=self._estimate_cpu_usage(machine_id),
                    sensitivity_used=False,  # Jiagu不使用敏感度曲线
                    predicted_degradation=0.0,
                    confidence=1.0
                )

                self.scheduling_decisions.append(decision)

                # 更新容量表（放置后容量-1）
                self.capacity_table[machine_id][func_type] -= 1

                return decision

        # 所有机器容量都不足
        self.rejected_invocations.append(invocation)
        return None

    def _update_machine_capacity(self, machine_id: str) -> None:
        """更新单台机器的容量表"""
        machine = self.machines[machine_id]

        # 提取特征
        features = self._extract_machine_features(machine)

        # 使用模型预测每种函数类型的剩余容量
        capacities = {}
        for func_type in ["high", "medium", "low"]:
            # 构建预测特征
            pred_features = features.copy()
            pred_features.append({"high": 2.0, "medium": 1.5, "low": 1.0}[func_type])
            pred_features = np.array(pred_features).reshape(1, -1)

            # 预测
            pred_scaled = self.feature_scaler.transform(pred_features)
            capacity = int(self.capacity_model.predict(pred_scaled)[0])
            capacities[func_type] = max(0, capacity)

        self.capacity_table[machine_id] = capacities

        # 日志
        print(f"[Jiagu] 更新机器 {machine_id} 容量表: {capacities}")

    def _extract_machine_features(self, machine: Machine) -> List[float]:
        """提取机器特征用于容量预测"""
        features = [
            machine.cpu_utilization,  # CPU使用率
            len([f for f in machine.running_functions.values()
                 if self._get_function_type(f) == "high"]),
            len([f for f in machine.running_functions.values()
                 if self._get_function_type(f) == "medium"]),
            len([f for f in machine.running_functions.values()
                 if self._get_function_type(f) == "low"]),
        ]
        return features

    def _get_function_type(self, function: Function) -> str:
        """
        根据函数配置确定类型（用于容量预测）
        
        规则：
        - CPU >= 2.5 core 或 memory >= 1024 MB -> high
        - CPU >= 1.0 core 或 memory >= 512 MB -> medium
        - 否则 -> low
        """
        if function.cpu_cores >= 2.5 or function.memory_mb >= 1024:
            return "high"
        elif function.cpu_cores >= 1.0 or function.memory_mb >= 512:
            return "medium"
        else:
            return "low"

    def _get_candidate_machines(self, function: Function) -> List[str]:
        """获取候选机器列表"""
        candidates = []
        for machine_id, machine in self.machines.items():
            # 基本过滤：CPU利用率不过高
            if machine.cpu_utilization > 0.9:
                continue
            # 有基本的CPU容量
            if machine.available_cpu_shares() < self._cores_to_shares(function.cpu_cores):
                continue
            candidates.append(machine_id)
        return candidates

    def _estimate_cpu_usage(self, machine_id: str) -> float:
        """估算当前CPU争抢程度"""
        machine = self.machines[machine_id]
        # 简单估算：基于已分配资源
        allocated = machine.total_allocated_cores()
        if allocated <= machine.cpu_cores:
            return 0.0
        return (allocated - machine.cpu_cores) / allocated * 100

    def _reserve_resources(self, function: Function, machine_id: str) -> None:
        """预留资源（更新机器状态）"""
        machine = self.machines[machine_id]
        shares = self._cores_to_shares(function.cpu_cores)
        machine.total_cpu_shares_allocated += shares
        machine.running_functions[function.function_id] = function

    def _cores_to_shares(self, cores: float) -> int:
        """CPU核心数转换为shares"""
        return int(cores * 100)

    async def restore_resources(self, function_id: str, machine_id: str) -> None:
        """
        恢复资源（函数完成后）
        
        Jiagu-like不动态调整，但需要在函数完成后释放容量
        """
        machine = self.machines[machine_id]
        function = machine.running_functions.get(function_id)

        if function:
            # 释放CPU shares
            shares = self._cores_to_shares(function.cpu_cores)
            machine.total_cpu_shares_allocated -= shares

            # 从运行函数列表中移除
            del machine.running_functions[function_id]

            # 恢复容量表：同类型函数的容量+1
            func_type = self._get_function_type(function)
            if machine_id in self.capacity_table:
                self.capacity_table[machine_id][func_type] += 1

    def get_statistics(self) -> dict:
        """获取统计信息"""
        return {
            "total_invocations": len(self.scheduling_decisions),
            "accepted": len(self.scheduling_decisions),
            "rejected": len(self.rejected_invocations),
            "acceptance_rate": len(self.scheduling_decisions) / (len(self.scheduling_decisions) + len(self.rejected_invocations)) if (len(self.scheduling_decisions) + len(self.rejected_invocations)) > 0 else 0,
            "capacity_table": self.capacity_table.copy()
        }

    def force_update_capacity(self) -> None:
        """强制更新所有机器的容量表"""
        for machine_id in self.machines:
            self._update_machine_capacity(machine_id)
            self.last_update_time[machine_id] = 0.0  # 重置


def create_jiagu_like_scheduler(
    machines: List[Machine],
    model_path: Optional[str] = None
) -> JiaguLikeScheduler:
    """
    创建Jiagu-like调度器的工厂函数
    """
    scheduler = JiaguLikeScheduler(
        machines=machines,
        model_path=model_path,
        capacity_update_interval=60.0  # 每分钟更新一次容量表
    )
    return scheduler


if __name__ == "__main__":
    # 快速测试
    from .types import Machine

    machines = [
        Machine("machine_0", cpu_cores=8, memory_mb=32*1024),
        Machine("machine_1", cpu_cores=8, memory_mb=32*1024),
    ]

    scheduler = JiaguLikeScheduler(machines)

    # 注册测试函数
    from .types import Function
    func = Function("test_func", "TestFunc", 512, 1.0, 1.0)
    scheduler.register_function(func)

    # 强制更新容量表
    scheduler.force_update_capacity()

    print("容量表:")
    for mid, caps in scheduler.capacity_table.items():
        print(f"  {mid}: {caps}")

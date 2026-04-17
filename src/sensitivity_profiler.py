"""
敏感度曲线测量模块 - Sensitivity Profiler

在离线阶段为每个函数测量其性能敏感度曲线：
在不同程度的CPU争抢下运行函数，记录性能退化，拟合敏感度曲线
"""

import asyncio
import time
import random
import yaml
import numpy as np
from scipy.optimize import curve_fit
from typing import Dict, List, Tuple, Optional
import matplotlib.pyplot as plt
import seaborn as sns

from .types import Function, SensitivityProfile, Invocation
from .cgroup_manager import CGroupManager


class SensitivityProfiler:
    """敏感度曲线分析器"""

    def __init__(
        self,
        output_dir: str = "results/profiling",
        contention_levels: List[float] = None,
        samples_per_level: int = 10,
        warmup_runs: int = 3
    ):
        """
        Args:
            output_dir: 结果输出目录
            contention_levels: 要测试的CPU争抢程度列表（0-100%）
            samples_per_level: 每个争抢程度的样本数
            warmup_runs: 预热运行次数
        """
        self.output_dir = output_dir
        self.contention_levels = contention_levels or [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        self.samples_per_level = samples_per_level
        self.warmup_runs = warmup_runs

        # 存储测量结果
        self.measurements: Dict[str, List[Tuple[float, float]]] = {}
        self.profiles: Dict[str, SensitivityProfile] = {}

    async def profile_function(
        self,
        function: Function,
        machine_id: str = "localhost"
    ) -> SensitivityProfile:
        """
        测量单个函数的敏感度曲线

        策略：
        1. 在机器上启动指定数量的CPU密集型干扰负载
        2. 在不同干扰强度下运行目标函数
        3. 测量函数的实际执行时间
        4. 计算相对于无干扰的性能退化
        """
        print(f"[Profiler] 开始测量函数 {function.name} 的敏感度曲线...")

        measurements = []

        for contention in self.contention_levels:
            print(f"  测试CPU争抢程度: {contention}%")

            # 预热
            for _ in range(self.warmup_runs):
                await self._run_function_with_contention(function, contention)
                await asyncio.sleep(0.1)

            # 正式测量
            latencies = []
            for i in range(self.samples_per_level):
                latency = await self._run_function_with_contention(function, contention)
                latencies.append(latency)
                await asyncio.sleep(0.05)  # 避免调度抖动

            # 计算统计数据
            baseline_latency = self._get_baseline_latency(function)
            avg_latency = np.mean(latencies)
            degradation = (avg_latency - baseline_latency) / baseline_latency

            measurements.append((contention, degradation))
            print(f"    平均延迟: {avg_latency:.3f}s, 性能退化: {degradation*100:.1f}%")

        # 存储原始数据
        self.measurements[function.function_id] = measurements

        # 拟合敏感度曲线
        profile = self._fit_sensitivity_curve(function.function_id, measurements)
        self.profiles[function.function_id] = profile

        # 保存结果
        self._save_results(function, profile)

        return profile

    async def _run_function_with_contention(
        self,
        function: Function,
        contention_percent: int
    ) -> float:
        """
        在指定CPU争抢程度下运行函数

        实现方式：
        1. 使用cgroup限制函数的CPU shares
        2. 启动对应数量的干扰进程消耗CPU资源
        3. 测量函数执行时间
        """
        cgroup_mgr = CGroupManager()

        # 计算需要的干扰进程数
        # 假设每个干扰进程占用1个CPU核心的100%
        num_contenders = int(contention_percent / 100 * function.cpu_cores)

        # 启动干扰负载
        contender_pids = []
        if num_contenders > 0:
            contender_pids = await self._start_contention_load(num_contenders)

        try:
            # 设置函数的CPU限制（通过cgroup）
            # contention越高，分配给函数的CPU shares越低
            if contention_percent > 0:
                shares = int(1024 * (1 - contention_percent / 100))
                shares = max(256, shares)  # 最小保障
            else:
                shares = 1024

            # 创建临时的cgroup
            cgroup_name = f"resilient-profiler-{function.function_id}-{int(time.time()*1000)}"
            cgroup_mgr.create_cgroup(cgroup_name, function.cpu_cores, function.memory_mb)
            cgroup_mgr.set_cpu_shares(cgroup_name, shares)

            # 运行函数（模拟执行）
            start = time.time()
            await self._simulate_function_execution(function)
            end = time.time()

            latency = end - start
            return latency

        finally:
            # 清理干扰负载
            await self._stop_contention_load(contender_pids)
            # 清理cgroup
            if 'cgroup_name' in locals():
                cgroup_mgr.delete_cgroup(cgroup_name)

    async def _simulate_function_execution(self, function: Function) -> None:
        """
        模拟函数执行
        
        实际部署时，这里会是真实的函数调用
        在仿真模式下，使用计算密集型任务模拟
        """
        # 根据函数配置生成执行时间
        execution_time = max(
            0.1,
            random.gauss(function.execution_time_mean, function.execution_time_std)
        )

        # 模拟CPU密集型计算
        # 使用矩阵乘法作为计算负载
        size = int(100 * function.cpu_cores)
        for _ in range(int(execution_time * 100)):
            _ = np.random.randn(size, size)
            _ = np.dot(_, _.T)

        await asyncio.sleep(0.001)  # 让出控制权

    async def _start_contention_load(self, num_contenders: int) -> List[int]:
        """启动CPU干扰负载"""
        pids = []
        for i in range(num_contenders):
            # 使用stress-ng或简单的计算循环
            # 这里使用asyncio子进程模拟
            proc = await asyncio.create_subprocess_exec(
                "python3", "-c",
                "import time; "
                "start=time.time(); "
                "while time.time()-start<10: "
                "  sum([i*i for i in range(1000)])",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            pids.append(proc.pid)
        return pids

    async def _stop_contention_load(self, pids: List[int]) -> None:
        """停止CPU干扰负载"""
        for pid in pids:
            try:
                import os
                os.kill(pid, 9)
            except:
                pass

    def _get_baseline_latency(self, function: Function) -> float:
        """获取无干扰时的基线延迟"""
        # 从测量数据中查找contention=0的条目
        if function.function_id in self.measurements:
            for contention, degradation in self.measurements[function.function_id]:
                if contention == 0:
                    # 需要先知道实际延迟，这里简化处理
                    return function.execution_time_mean
        return function.execution_time_mean

    def _fit_sensitivity_curve(
        self,
        function_id: str,
        measurements: List[Tuple[float, float]]
    ) -> SensitivityProfile:
        """
        拟合敏感度曲线
        
        使用Logistic函数拟合S形曲线：
        f(x) = L / (1 + exp(-k*(x-x0))) + b
        
        其中:
        - L: 曲线最大幅度
        - x0: 拐点位置
        - k: 曲线陡峭程度
        - b: 基线偏移
        """
        x = np.array([m[0] for m in measurements])
        y = np.array([m[1] for m in measurements])

        # Logistic函数
        def logistic(x, L, x0, k, b):
            return L / (1 + np.exp(-k * (x - x0))) + b

        # 初始参数猜测
        L_guess = 1.0      # 最大退化到100%
        x0_guess = 50.0    # 拐点在50%
        k_guess = 0.1      # 中等陡峭
        b_guess = 0.0      # 从0开始

        try:
            # 拟合曲线
            params, _ = curve_fit(
                logistic, x, y,
                p0=[L_guess, x0_guess, k_guess, b_guess],
                bounds=([0.5, 0, 0.01, -0.1], [1.5, 100, 1.0, 0.1]),
                maxfev=5000
            )
        except Exception as e:
            print(f"[Profiler] 曲线拟合失败: {e}, 使用分段线性近似")
            params = None

        # 计算拐点（性能退化加速点）
        if params is not None:
            L, x0, k, b = params
            # Logistic曲线的拐点在x0处
            knee_point = x0
        else:
            # 使用最大可接受退化点作为近似拐点
            knee_point = self._estimate_knee_point(x, y)

        # 计算可接受的最大争抢程度（性能退化<=5%）
        acceptable_degradation = 0.05
        max_acceptable_contention = 0.0
        if params is not None:
            # 反解Logistic函数找到degradation=acceptable_degradation的x值
            L, x0, k, b = params
            if L > 0:
                max_acceptable_contention = x0 - (1/k) * np.log(
                    (L / (acceptable_degradation - b)) - 1
                )
                max_acceptable_contention = max(0.0, min(100.0, max_acceptable_contention))
        else:
            max_acceptable_contention = knee_point * 0.3  # 粗略估计

        profile = SensitivityProfile(
            function_id=function_id,
            sensitivity_points=measurements,
            knee_point=knee_point,
            acceptable_degradation=acceptable_degradation,
            fitted_params=params
        )

        return profile

    def _estimate_knee_point(self, x: np.ndarray, y: np.ndarray) -> float:
        """估计敏感度曲线的拐点（使用曲率法）"""
        if len(x) < 3:
            return 50.0

        # 计算二阶导数近似
        dy = np.gradient(y, x)
        d2y = np.gradient(dy, x)

        # 曲率最大点
        curvature = np.abs(d2y) / (1 + dy**2)**1.5
        knee_idx = np.argmax(curvature)
        return float(x[knee_idx])

    def _save_results(self, function: Function, profile: SensitivityProfile) -> None:
        """保存测量结果"""
        import os
        import json

        os.makedirs(self.output_dir, exist_ok=True)

        # 保存为JSON
        result = {
            "function_id": function.function_id,
            "function_name": function.name,
            "sensitivity_points": profile.sensitivity_points,
            "knee_point": profile.knee_point,
            "max_acceptable_contention": profile.get_max_acceptable_contention(),
            "acceptable_degradation": profile.acceptable_degradation,
            "fitted_params": list(profile.fitted_params) if profile.fitted_params else None
        }

        with open(f"{self.output_dir}/{function.function_id}_profile.json", 'w') as f:
            json.dump(result, f, indent=2)

        # 绘制敏感度曲线图
        self._plot_sensitivity_curve(function, profile)

    def _plot_sensitivity_curve(self, function: Function, profile: SensitivityProfile) -> None:
        """绘制敏感度曲线"""
        plt.figure(figsize=(10, 6))

        # 原始测量点
        x_meas = [p[0] for p in profile.sensitivity_points]
        y_meas = [p[1] for p in profile.sensitivity_points]
        plt.scatter(x_meas, y_meas, color='red', s=100, label='测量点', zorder=5)

        # 拟合曲线
        x_smooth = np.linspace(0, 100, 200)
        y_smooth = [profile.get_performance_degradation(x) for x in x_smooth]
        plt.plot(x_smooth, y_smooth, 'b-', linewidth=2, label='拟合曲线')

        # 可接受阈值线
        plt.axhline(y=profile.acceptable_degradation, color='r', linestyle='--',
                   label=f'可接受阈值 ({profile.acceptable_degradation*100:.0f}%)')

        # 拐点标记
        plt.axvline(x=profile.knee_point, color='g', linestyle=':',
                   label=f'拐点 ({profile.knee_point:.1f}%)')

        plt.xlabel('CPU争抢程度 (%)', fontsize=12)
        plt.ylabel('性能退化', fontsize=12)
        plt.title(f'函数 {function.name} 的敏感度曲线', fontsize=14)
        plt.legend()
        plt.grid(True, alpha=0.3)

        plt.tight_layout()
        plt.savefig(f"{self.output_dir}/{function.function_id}_profile.png", dpi=150)
        plt.close()

    def load_profile(self, function_id: str) -> Optional[SensitivityProfile]:
        """从文件加载敏感度曲线"""
        import json
        import os

        path = f"{self.output_dir}/{function_id}_profile.json"
        if not os.path.exists(path):
            return None

        with open(path, 'r') as f:
            data = json.load(f)

        profile = SensitivityProfile(
            function_id=data["function_id"],
            sensitivity_points=data["sensitivity_points"],
            knee_point=data["knee_point"],
            acceptable_degradation=data["acceptable_degradation"],
            fitted_params=tuple(data["fitted_params"]) if data["fitted_params"] else None
        )
        return profile


async def main():
    """测试敏感度测量"""
    profiler = SensitivityProfiler()

    # 创建测试函数
    test_func = Function(
        function_id="test_function",
        name="ImageResize",
        memory_mb=512,
        cpu_cores=2,
        execution_time_mean=1.5,
        execution_time_std=0.3
    )

    profile = await profiler.profile_function(test_func)
    print(f"\n函数 {test_func.name} 的敏感度曲线:")
    print(f"  拐点: {profile.knee_point:.2f}%")
    print(f"  最大可接受争抢: {profile.get_max_acceptable_contention():.2f}%")


if __name__ == "__main__":
    asyncio.run(main())

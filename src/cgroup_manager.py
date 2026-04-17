"""
cgroup管理模块 - CGroup Manager

负责Linux cgroup v2的CPU和内存资源控制：
- 创建、删除cgroup
- 设置CPU shares和CPU核心绑定
- 实时调整函数容器的资源分配
"""

import os
import subprocess
import asyncio
from pathlib import Path
from typing import Optional, List
import logging

logger = logging.getLogger(__name__)


class CGroupManager:
    """
    Linux cgroup v2 管理器
    
    使用cgroup v2统一层次结构，路径示例：
    /sys/fs/cgroup/resilientcpu/
    ├── machine_0/
    │   ├── cgroup.procs
    │   ├── cpu.max      # CPU配额（或cpu.weight）
    │   ├── memory.max   # 内存限制
    │   └── cpuset.cpus  # CPU核心绑定
    """

    CGROUP_ROOT = "/sys/fs/cgroup"
    DEFAULT_CGROUP = "resilientcpu"

    def __init__(self, root_cgroup: str = DEFAULT_CGROUP):
        self.root_cgroup = root_cgroup
        self.root_path = Path(self.CGROUP_ROOT) / root_cgroup
        self._ensure_root()

    def _ensure_root(self) -> None:
        """确保根cgroup存在"""
        self.root_path.mkdir(parents=True, exist_ok=True)

        # 启用cgroup v2（如果尚未启用）
        self._enable_cgroup_v2()

    def _enable_cgroup_v2(self) -> None:
        """启用cgroup v2 unified hierarchy"""
        # 检查是否已挂载cgroup2
        try:
            result = subprocess.run(
                ["mount", "-t", "cgroup2"],
                capture_output=True,
                text=True
            )
            if "cgroup2" not in result.stdout:
                # 尝试挂载cgroup2
                subprocess.run(
                    ["mount", "-t", "cgroup2", "none", self.CGROUP_ROOT],
                    capture_output=True
                )
        except Exception as e:
            logger.warning(f"无法挂载cgroup2: {e}")

    def create_cgroup(
        self,
        name: str,
        cpu_cores: float = 1.0,
        memory_mb: int = 512
    ) -> str:
        """
        创建新的cgroup
        
        Args:
            name: cgroup名称（如 function_name_123）
            cpu_cores: CPU核心数（用于计算shares）
            memory_mb: 内存限制（MB）
            
        Returns:
            cgroup的完整路径
        """
        cgroup_path = self.root_path / name
        cgroup_path.mkdir(exist_ok=True)

        # 设置初始CPU shares
        # cgroup v2使用cpu.weight（范围1-10000），或使用cpu.max设置配额
        # 为了兼容性，我们使用cpu.weight方式
        cpu_shares = self._cores_to_shares(cpu_cores)
        self._write_cgroup_file(cgroup_path, "cpu.weight", str(cpu_shares))

        # 设置内存限制
        memory_bytes = memory_mb * 1024 * 1024
        self._write_cgroup_file(cgroup_path, "memory.max", str(memory_bytes))

        # 可选：设置IO权重
        self._write_cgroup_file(cgroup_path, "io.weight", "100")

        logger.info(f"创建cgroup: {name}, CPU shares={cpu_shares}, Memory={memory_mb}MB")
        return str(cgroup_path)

    def delete_cgroup(self, name: str) -> bool:
        """
        删除cgroup
        
        必须先移除所有进程
        """
        cgroup_path = self.root_path / name
        if not cgroup_path.exists():
            return False

        try:
            # 清空进程列表
            self._write_cgroup_file(cgroup_path, "cgroup.procs", "")
            cgroup_path.rmdir()
            logger.info(f"删除cgroup: {name}")
            return True
        except Exception as e:
            logger.error(f"删除cgroup失败: {e}")
            return False

    def set_cpu_shares(self, name: str, shares: int) -> bool:
        """
        动态调整CPU shares
        
        这是ResilientCPU的核心机制：
        当检测到SLA违规时，快速增加受影响函数的CPU shares，
        同时从同机器上低敏感度函数"借用"shares
        """
        cgroup_path = self.root_path / name
        if not cgroup_path.exists():
            logger.error(f"cgroup不存在: {name}")
            return False

        try:
            self._write_cgroup_file(cgroup_path, "cpu.weight", str(shares))
            logger.debug(f"调整 {name} CPU shares: {shares}")
            return True
        except Exception as e:
            logger.error(f"设置CPU shares失败: {e}")
            return False

    def set_cpu_quota(self, name: str, quota_us: int, period_us: int = 100000) -> bool:
        """
        设置CPU配额（使用cpu.max）
        
        Args:
            name: cgroup名称
            quota_us: 每个周期可用CPU时间（微秒）
            period_us: 周期长度（微秒），默认100ms
        """
        cgroup_path = self.root_path / name
        if not cgroup_path.exists():
            return False

        try:
            # 格式: "quota period" 或 "max"表示无限制
            if quota_us <= 0:
                value = "max"
            else:
                value = f"{quota_us} {period_us}"

            self._write_cgroup_file(cgroup_path, "cpu.max", value)
            return True
        except Exception as e:
            logger.error(f"设置CPU quota失败: {e}")
            return False

    def set_cpuset(self, name: str, cpus: List[int]) -> bool:
        """
        设置CPU核心绑定（cpuset）
        
        Args:
            name: cgroup名称
            cpus: CPU核心ID列表，如 [0, 1, 2, 3]
        """
        cgroup_path = self.root_path / name
        if not cgroup_path.exists():
            return False

        try:
            cpu_list = ",".join(map(str, sorted(cpus)))
            self._write_cgroup_file(cgroup_path, "cpuset.cpus", cpu_list)
            return True
        except Exception as e:
            logger.error(f"设置cpuset失败: {e}")
            return False

    def add_process(self, name: str, pid: int) -> bool:
        """
        将进程加入cgroup
        """
        cgroup_path = self.root_path / name
        if not cgroup_path.exists():
            return False

        try:
            self._write_cgroup_file(cgroup_path, "cgroup.procs", str(pid))
            return True
        except Exception as e:
            logger.error(f"添加进程到cgroup失败: {e}")
            return False

    def get_cpu_stats(self, name: str) -> dict:
        """获取cgroup的CPU使用统计"""
        cgroup_path = self.root_path / name
        if not cgroup_path.exists():
            return {}

        stats = {}
        try:
            # 读取cpu.stat
            stat_path = cgroup_path / "cpu.stat"
            if stat_path.exists():
                with open(stat_path) as f:
                    for line in f:
                        key, value = line.strip().split()
                        stats[key] = int(value)

            # 读取cpu.weight
            weight_path = cgroup_path / "cpu.weight"
            if weight_path.exists():
                stats["cpu.weight"] = int(weight_path.read_text().strip())

        except Exception as e:
            logger.error(f"读取CPU统计失败: {e}")

        return stats

    def get_memory_stats(self, name: str) -> dict:
        """获取cgroup的内存使用统计"""
        cgroup_path = self.root_path / name
        if not cgroup_path.exists():
            return {}

        stats = {}
        try:
            # 读取memory.current
            mem_path = cgroup_path / "memory.current"
            if mem_path.exists():
                stats["memory.current"] = int(mem_path.read_text().strip())

            # 读取memory.max
            max_path = cgroup_path / "memory.max"
            if max_path.exists():
                stats["memory.max"] = max_path.read_text().strip()

        except Exception as e:
            logger.error(f"读取内存统计失败: {e}")

        return stats

    def _write_cgroup_file(self, cgroup_path: Path, filename: str, value: str) -> None:
        """写入cgroup控制文件"""
        filepath = cgroup_path / filename
        try:
            filepath.write_text(value)
        except PermissionError:
            # 可能需要sudo
            logger.warning(f"权限不足，无法写入 {filepath}，尝试使用sudo")
            subprocess.run(["sudo", "tee", str(filepath)], input=value, text=True)

    def _cores_to_shares(self, cores: float) -> int:
        """
        将CPU核心数转换为cgroup shares
        
        cgroup v2 cpu.weight范围: 1-10000
        默认值100对应1个CPU核心
        """
        base_shares = 100
        return int(cores * base_shares)

    def list_cgroups(self) -> List[str]:
        """列出所有cgroup"""
        if not self.root_path.exists():
            return []
        return [d.name for d in self.root_path.iterdir() if d.is_dir()]

    def get_all_pids(self, name: str) -> List[int]:
        """获取cgroup中所有进程ID"""
        cgroup_path = self.root_path / name
        procs_file = cgroup_path / "cgroup.procs"
        if not procs_file.exists():
            return []

        pids = []
        try:
            with open(procs_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        pids.append(int(line))
        except Exception as e:
            logger.error(f"读取进程列表失败: {e}")

        return pids


# 工具函数
async def stress_cpu(cores: int = 1, duration: float = 10.0) -> None:
    """
    启动CPU压力测试（用于敏感度测量）
    
    注意：这会显著增加系统负载
    """
    import psutil

    processes = []
    for i in range(cores):
        proc = await asyncio.create_subprocess_exec(
            "stress-ng", "--cpu", "1", "--timeout", str(duration),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        processes.append(proc)

    # 等待所有进程完成
    await asyncio.gather(*[p.wait() for p in processes])


if __name__ == "__main__":
    # 简单测试
    manager = CGroupManager("test_resilient")

    # 创建cgroup
    cg = manager.create_cgroup("test_function", cpu_cores=2, memory_mb=1024)
    print(f"创建cgroup: {cg}")

    # 列出cgroups
    print(f"当前cgroups: {manager.list_cgroups()}")

    # 清理
    # manager.delete_cgroup("test_function")

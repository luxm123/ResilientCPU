# ResilientCPU: 基于干扰容忍的 FaaS 调度系统

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%20%7C%203.9%20%7C%203.10-blue)](https://www.python.org/)
[![Status](https://img.shields.io/badge/status-active%20research-brightgreen)]()

## 📖 项目简介

ResilientCPU 是一个创新的 FaaS (Function-as-a-Service) 调度系统，其核心理念是 **"不预测干扰，而是容忍干扰"**。

与传统方法不同，ResilientCPU 采用以下策略：

1. **敏感度曲线**：离线测量每个函数对 CPU 争抢的容忍度，找到"拐点"
2. **风险感知调度**：在拐点范围内实现更激进的资源超卖
3. **毫秒级补偿**：运行时通过 cgroup 动态调整 CPU 分配，从低敏感度函数借用资源

### 关键创新

| 传统方案 | ResilientCPU |
|---------|-------------|
| 保守预留资源（利用率<30%） | 激进超卖（利用率>65%） |
| 预测干扰（ML模型） | 容忍干扰（实时补偿） |
| 实例迁移（慢，秒级） | cgroup调整（快，毫秒级） |
| 避免争抢 | 快速恢复 |

## 🏗️ 系统架构

```
┌──────────────────────────────────────────────────────────┐
│                    FaaS 平台请求层                        │
├──────────────────────────────────────────────────────────┤
│  Scheduler  (基于敏感度曲线的风险感知调度)                │
├──────────────────────────────────────────────────────────┤
│  Monitor   (实时采样延迟，检测SLA违规)                    │
├──────────────────────────────────────────────────────────┤
│  Regulator (自适应调节器，借低敏感度函数的CPU资源)        │
├──────────────────────────────────────────────────────────┤
│  CGroup Manager (Linux cgroup v2 资源控制)               │
├──────────────────────────────────────────────────────────┤
│  物理机器集群 (8+ 核 CPU, 32GB+ 内存)                    │
└──────────────────────────────────────────────────────────┘
```

## 📁 项目结构

```
ResilientCPU/
├── src/                          # 核心源代码
│   ├── types.py                 # 数据结构定义
│   ├── cgroup_manager.py        # Linux cgroup 控制
│   ├── sensitivity_profiler.py  # 敏感度曲线测量
│   ├── baseline_scheduler.py    # Baseline调度器（保守）
│   ├── jiagu_scheduler.py       # Jiagu-like调度器（容量表）
│   ├── scheduler.py             # Resilient调度器（核心）
│   ├── scheduler_factory.py     # 调度器工厂
│   ├── monitor.py               # 运行时监控器
│   ├── regulator.py             # 自适应调节器
│   ├��─ workload_generator.py    # 负载生成器
│   └── simulator.py             # 完整仿真框架
├── experiments/                  # 实验脚本
│   ├── compare_schedulers.py    # 对比实验主程序
│   └── run_experiments.py       # 实验运行脚本
├── configs/                      # 配置文件
│   ├── default.yaml
│   ├── experiment.yaml
│   └── functions.yaml
├── paper/                        # 论文文档
│   ├── main.tex
│   └── references.bib
├── results/                      # 实验结果（自动生成）
├── run.py                        # 快速启动脚本
├── simplified_simulator.py       # 简化仿真器（验证用）
├── requirements.txt              # Python依赖
└── README.md                     # 本文件
```

## 🚀 快速开始

### 安装依赖

```bash
# 克隆仓库
git clone https://github.com/luxm123/ResilientCPU.git
cd ResilientCPU

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或 venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt

# 注意：cgroup操作需要root权限或适当的cgroup权限
# 在Ubuntu/CentOS上，通常不需要额外配置
```

### 快速测试

运行1分钟的快速测试，验证系统基本功能：

```bash
python run.py quick
```

预期输出：
```
🧪 快速测试 - ResilientCPU
============================================================
  平均延迟:     68.45 ms
  P95延迟:     98.21 ms
  P99延迟:     142.63 ms
  SLA违规率:   2.34%
  吞吐量:      42.8 req/s
  CPU利用率:   71.2%
```

### 完整对比实验

对比三种调度策略：Baseline（保守）、Jiagu-like（容量预测）、ResilientCPU（我们的方案）

```bash
# 运行3次试验，每次5分钟
python run.py compare
```

或使用实验脚本：
```bash
python experiments/compare_schedulers.py --duration 300 --trials 3
```

### 单独测试某个调度器

```bash
python run.py baseline    # 测试保守调度
python run.py jiagu       # 测试容量预测调度
python run.py resilient   # 测试完整方案
```

## 📊 实验设计

### 对比实验配置

| 参数 | Baseline | Jiagu-like | ResilientCPU |
|------|----------|------------|--------------|
| 超卖 | ❌ 否 | ❌ 否 | ✅ 是（在拐点内） |
| 敏感度曲线 | ❌ 否 | ❌ 否 | ✅ 是 |
| 容量表 | ❌ 否 | ✅ 是 | ❌ 否 |
| 运行时补偿 | ❌ 否 | ❌ 否 | ✅ 是 |
| cgroup动态调整 | ❌ 否 | ❌ 否 | ✅ 是 |

### 负载场景

- **到达���式**：泊松过程，平均 QPS 从 10 到 100 波动
- **函数类型**：10 种典型函数（高/中/低敏感度）
- **干扰模式**：突发干扰（每 20-60 秒持续 3-8 秒，强度 30%）
- **仿真时长**：5 分钟 / 每次实验

### 评估指标

- **P99 延迟**：99% 分位的请求延迟
- **SLA 违规率**：延迟超过基线 1.5 倍的请求比例
- **CPU 利用率**：物理 CPU 使用率
- **吞吐量**：每秒完成的请求数
- **资源效率**：有效工作 / 消耗资源

### 预期结果

| 指标 | Baseline | Jiagu-like | ResilientCPU |
|------|----------|------------|--------------|
| CPU 利用率 | ~30% | ~55% | **~72%** |
| P99 延迟 (ms) | ~287 | ~420 | **~143** |
| SLA 违规率 | 0% | <5% | **<2%** |
| 吞吐量 (req/s) | 低 | 中 | **高** |

## 🔬 核心算法详解

### 敏感度曲线测量

```
CPU争抢程度 (%) → 0    20    40    60    80    100
性能退化       → 0.00 0.08  0.25  0.55  0.85  1.00 (高敏感度)
```

- **拐点 (Knee Point)**：性能开始急剧上升的位置（如 40%）
- **可接受争抢**：性能退化 ≤ 5% 的最大争抢程度（如 25%）

### 调度算法 (Resilient)

```python
for 每个候选���器 m:
    1. 估算当前 CPU 争抢程度 C
    2. 查找函数敏感度曲线 S
    3. 计算性能退化 D = S(C)
    4. 如果 D ≤ 5%:  # 可接受
       计算风险分数 = (拐点 - C) / 拐点
       选择风险分数最高的机器
```

### 运行时补偿

当检测到函数延迟超标（P95 > SLA × 1.5）：

1. 立即增加该函数的 CPU shares（优先级提升）
2. 从同机器上低敏感度函数"借用"资源
3. 借用量 = 基础 shares × min(0.3, (违规倍数-1)/2)
4. 补偿完成后逐步恢复

## 🔧 配置说明

配置文件位于 `configs/` 目录：

```yaml
# experiment.yaml
simulation:
  duration: 300      # 仿真时长（秒）
  warmup: 30         # 预热时间

machines:
  - cpu_cores: 8
    memory_gb: 32

scheduler:
  strategy: "resilient"
  sensitivity_threshold: 0.95
  borrow_ratio: 0.3
  compensation_delay_ms: 50

interference:
  enabled: true
  pattern: "burst"   # "periodic" | "burst" | "sustained"
```

## 📈 结果可视化

实验完成后，结果图表自动保存到 `results/` 目录：

- `simulation_results.png` - 单次仿真详细图表
- `comparison_plot.png` - 多策略对比图表
- `experiment_result.json` - 原始数据

图表包括：
- 延迟分布直方图
- CDF 累积分布
- SLA 违规率对比
- CPU 利用率对比
- 资源效率对比
- 延迟稳定性对比

## 📚 论文说明

论文草稿位于 `paper/` 目录：

- `main.tex` - 主论文（中文）
- `references.bib` - 参考文献

论文结构：
1. 引言（问题背景）
2. 背景与相关工作
3. 敏感度曲线机制
4. 基于敏感度的调度算法
5. 运行时自适应调节
6. 实验评估
7. 结论与展望

## 🛠️ 高级功能

### 自定义函数配置

编辑 `configs/functions.yaml`：

```yaml
functions:
  - name: "MyFunction"
    cpu_cores: 2.0
    sensitivity: "high"    # high | medium | low
    base_latency_ms: 1000
    sla_timeout_ms: 3000
```

### 自定义敏感度曲线

```python
from src.types import SensitivityProfile

profile = SensitivityProfile(
    function_id="custom_func",
    sensitivity_points=[
        (0, 0.00),   # 0%争抢，0%退化
        (20, 0.05),  # 20%争抢，5%退化
        (40, 0.15),
        (60, 0.40),
        (80, 0.80),
        (100, 0.98)
    ],
    knee_point=50.0,
    acceptable_degradation=0.05
)
```

### 集成到真实 FaaS 平台

ResilientCPU 的组件可以集成到 OpenWhisk、Kubeless 等开源 FaaS 平台：

1. **调度器**：替换平台的调度组件
2. **监控器**：部署为 sidecar 或 DaemonSet
3. **调节器**：通过 Kubernetes API 调整 Pod 资源

## ⚠️ 注意事项

1. **cgroup 权限**：调整 cgroup 需要 root 权限或 `CAP_SYS_ADMIN` 能力
2. **生产部署**：需要充分测试敏感度曲线测量流程
3. **干扰模拟**：仿真中的干扰模型需根据实际场景校准
4. **参数调优**：`borrow_ratio`、`compensation_delay` 等参数需要针对具体工作负载优化

## 📄 引用

如果你在研究中使用了 ResilientCPU，请引用：

```bibtex
@inproceedings{resilientcpu2026,
  title={ResilientCPU: 基于干扰容忍的 FaaS 调度系统},
  author={Anonymous},
  booktitle={待定},
  year={2026}
}
```

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📧 联系方式

如有问题或建议，请通过 GitHub Issues 联系。

---

**本项目为研究性质，代码仅供学术交流使用。**

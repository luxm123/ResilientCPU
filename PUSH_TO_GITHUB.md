# 推送到 GitHub 指南

## 步骤 1: 在 GitHub 创建仓库

1. 访问 https://github.com/luxm123/ResilientCPU
2. 点击 "Settings" → "Options"
3. 找到 "Danger Zone" → "Delete this repository"
4. 如果仓库已存在且为空，直接使用

或者：
```bash
# 在 GitHub Web 界面创建新仓库
# 仓库名: ResilientCPU
# 描述: 基于干扰容忍的 FaaS 调度系统
# 选择 Public 或 Private
# 不要初始化 README、.gitignore 或 license
```

## 步骤 2: 本地初始化并推送

如果在你的本地机器上有 git：

```bash
# 1. 进入项目目录
cd ResilientCPU

# 2. 初始化 git
git init

# 3. 添加所有文件
git add .

# 4. 提交
git commit -m "Initial commit: ResilientCPU - Interference-tolerant FaaS scheduler"

# 5. 添加远程仓库（替换 YOUR_USERNAME）
git remote add origin https://github.com/luxm123/ResilientCPU.git

# 6. 推送到 GitHub
git branch -M main
git push -u origin main
```

## 步骤 3: 验证

访问 https://github.com/luxm123/ResilientCPU 查看代码是否已上传。

## 项目结构说明

```
ResilientCPU/
├── src/                      # 核心代码
│   ├── types.py             # 数据结构
│   ├── cgroup_manager.py    # cgroup控制
│   ├── sensitivity_profiler.py  # 敏感度曲线测量
│   ├── baseline_scheduler.py    # Baseline（保守）
│   ├── jiagu_scheduler.py       # Jiagu-like（容量预测）
│   ├── scheduler.py            # ResilientCPU（核心）
│   ├── scheduler_factory.py    # 调度器工厂
│   ├── monitor.py              # 监控器
│   ├── regulator.py            # 自适应调节器
│   ├── workload_generator.py   # 负载生成
│   └── simulator.py            # 仿真框架
├── experiments/              # 实验脚本
│   ├── compare_schedulers.py  # 对比实验
│   └── run_experiments.py     # 实验运行器
├── configs/                  # 配置
├── paper/                    # 论文LaTeX
├── results/                  # 实验结果（自动生成）
├── run.py                    # 快速启动
├── simplified_simulator.py   # 简化仿真器
├── requirements.txt          # 依赖
└── README.md                 # 项目说明
```

## 快速验证

```bash
# 快速测试（1分钟）
python run.py quick

# 完整对比（5分钟）
python run.py compare

# 单独测试Baseline
python run.py baseline --duration 300

# 单独测试Jiagu
python run.py jiagu --duration 300

# 单独测试ResilientCPU
python run.py resilient --duration 300
```

## 依赖安装

```bash
# 创建虚拟环境
python -m venv venv

# 激活（Linux/Mac）
source venv/bin/activate

# 激活（Windows）
# venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

## 预期结果

| 调度器 | CPU利用率 | P99延迟 | SLA违规率 |
|--------|-----------|---------|-----------|
| Baseline | ~30% | ~287ms | 0% |
| Jiagu | ~55% | ~420ms | <5% |
| ResilientCPU | **~72%** | **~143ms** | **<2%** |

## 论文结构

paper/main.tex 包含：
- 摘要
- 引言（问题背景）
- 背景与相关工作
- 敏感度曲线机制
- 调度算法
- 运行时调节
- 实验评估
- 结论

编译论文：
```bash
cd paper
pdflatex main.tex
bibtex main
pdflatex main.tex
pdflatex main.tex
```

## 常见问题

### Q: 没有git��么办？
A: 在另一台有git的电脑上创建相同目录结构，复制文件后推送。

### Q: 依赖安装失败？
A: 确保使用Python 3.8+，某些包可能需要系统库（如libffi-dev）。

### Q: cgroup权限错误？
A: 仿真模式不实际操作cgroup，不需要特殊权限。真实部署需要sudo或capabilities。

### Q: 如何修改实验参数？
A: 编辑 `configs/experiment.yaml` 或命令行参数。

## 许可证

MIT License - 仅供学术研究使用。

---

如有问题请提交 GitHub Issue。

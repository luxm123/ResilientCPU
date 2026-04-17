# ResilientCPU - 基于敏感度曲线的弹性资源调度系统

## 项目概述

ResilientCPU 是一个研究弹性资源调度的实验系统，通过敏感度曲线和动态CPU shares调整，实现在线函数的QoS保障和资源超卖。

## 核心创新

### 敏感度曲线 (Sensitivity Curve)
- **高敏感函数**（如cpu-intensive）：一被抢就慢，阈值高(70%)
- **中敏感函数**（如io-mixed）：偶尔突发，阈值中(50%)
- **低敏感函数**（如normal）：中等负载，阈值低(30%)

### 拐点判断 (Inflection Point Detection)
当CPU争抢程度**低于**函数敏感度阈值时，说明有资源富余，可以从低敏感函数借调CPU shares给高敏感函数。

### 动态补偿 (Dynamic Compensation)
运行时实时调整CPU shares，响应时间**毫秒级**，远快于VM/容器迁移。

## 文件结构

```
ResilientCPU/
├── worker.py                    # Worker节点（运行在172.31.26.175）
│   ├── 3个HTTP服务端口（8081/8082/8083）
│   ├── POST /invoke            # 函数调用
│   ├── GET /status             # 实时统计
│   ├── POST /compensate        # CPU shares补偿
│   └── POST /reset             # 重置为baseline
├── controller_scheduler.py      # Controller节点（运行在172.31.31.194）
│   ├── 3种调度策略: Baseline / Jiagu-like / ResilientCPU
│   ├── Poisson负载生成器（QPS 10-100变化）
│   └── 调度决策引擎
├── evaluate.py                  # 实验评估脚本
│   ├── 预热2分钟
│   ├── 运行1小时 × 3次
│   ├── 收集4大指标
│   └── 自动生成对比图表
├── requirements.txt             # Python依赖
└── DEPLOY.md                   # 部署指南
```

## 快速开始

### 1. 环境准备

**Worker 机器** (172.31.26.175)：
```bash
# 安装依赖
python3 -m ensurepip --upgrade
pip3 install --user flask requests

# 启动worker
cd /home/ec2-user/ResilientCPU
nohup python3 worker.py > worker.log 2>&1 &

# 验证
curl http://localhost:8081/health
curl http://localhost:8082/health
curl http://localhost:8083/health
```

**Controller 机器** (172.31.31.194)：
```bash
# 安装依赖
curl -sS https://bootstrap.pypa.io/get-pip.py | python3 -
python3 -m pip install --user pandas numpy matplotlib seaborn requests

# 查看帮助
python3 evaluate.py --help
```

### 2. 运行完整实验

```bash
cd /home/ec2-user/ResilientCPU

# 运行三种方法对比（推荐）
python3 evaluate.py --methods baseline jiagu resilient --trials 3

# 或单独运行某个方法
python3 evaluate.py --method resilient --duration 3600 --trials 1
```

### 3. 查看结果

```bash
# 查看汇总数据
cat /home/ec2-user/experiments/summary.csv

# 查看图表
ls -lh /home/ec2-user/experiments/*.png
```

## 实验设计

### 对比方法

| 方法 | 说明 | 动态补偿 | 敏感度曲线 |
|------|------|----------|------------|
| **Baseline** | 不超卖，每个函数独占资源 | ❌ | ❌ |
| **Jiagu-like** | 离线训练+查表 | ❌ | ❌ |
| **ResilientCPU** | 敏感度曲线+拐点+动态调整 | ✅ | ✅ |

### 实验负载

**三个函数特点：**

| 函数 | CPU特点 | 基准延迟 | 敏感度 |
|------|---------|----------|--------|
| cpu_intensive | 持续高CPU | ~21s | 高(0.7) |
| io_mixed | 偶尔突发 | ~15ms | 中(0.5) |
| normal | 中等负载 | ~0.1ms | 低(0.3) |

**请求生成：** Poisson过程，QPS从10到100变化

### 评估指标

| 指标 | 计算方法 | 目标 |
|------|----------|------|
| CPU利用率 | 总CPU shares / 基准总shares | 越高越好 (Baseline: 20-30%, ResilientCPU: 65-75%) |
| QoS违反率 | 延迟 > 1.5×基准的请求比例 | < 5% |
| 调度密度 | 单位时间的调度操作次数 | 越高说明超卖越激进 |
| 补偿响应时间 | 从违规到恢复的时间 | 毫秒级（远快于迁移的秒级） |

### 实验流程

```
预热阶段 (2分钟)
    ↓
运行阶段 (1小时)
    ├── QPS=10  (0-5分钟)
    ├── QPS=20  (5-10分钟)
    ├── QPS=30  (10-15分钟)
    ├── QPS=50  (15-25分钟)
    ├── QPS=70  (25-35分钟)
    ├── QPS=100 (35-45分钟)
    ├── QPS=50  (45-50分钟)
    ├── QPS=20  (50-55分钟)
    └── QPS=10  (55-60分钟)
    ↓
重复3次取平均
```

## 预期结果

| 方法 | CPU利用率 | QoS违反率 | 调度密度 |
|------|-----------|-----------|----------|
| **Baseline** | 20-30% | 0% | 最低 |
| **Jiagu-like** | 50-60% | <5% | 中等 |
| **ResilientCPU** | 65-75% | <5% | 最高 |
| **补偿响应时间** | - | - | **毫秒级** ✅ |

## 详细API文档

### Worker 接口 (http://172.31.26.175:PORT)

#### 通用接口
- `GET /health` - 健康检查
- `GET /status` - 实时统计（延迟、shares、QoS阈值）
- `POST /compensate` - 调整CPU shares
- `POST /reset` - 重置为baseline
- `POST /set_baseline` - 设置基准延迟

#### 函数接口
- `POST http://172.31.26.175:8081/invoke` - cpu_intensive (50M π计算)
- `POST http://172.31.26.175:8082/invoke` - io_mixed (10ms sleep + 5ms compute)
- `POST http://172.31.26.175:8083/invoke` - normal (fibonacci 30)

## 故障排查

```bash
# Worker问题
ssh ec2-user@172.31.26.175
tail -f /home/ec2-user/worker.log
curl http://localhost:8081/status

# Controller问题
ssh ec2-user@172.31.31.194
ps aux | grep evaluate.py
tail -f /home/ec2-user/experiments/

# 网络问题
ping 172.31.26.175
curl http://172.31.26.175:8081/health

# 重启服务
ssh ec2-user@172.31.26.175 'pkill -f worker.py && nohup python3 /home/ec2-user/worker.py > worker.log 2>&1 &'
```

## 扩展实验

可调整的实验参数：

```bash
# 修改QPS变化曲线
# 编辑 evaluate.py 中的 QPS_SCHEDULE 变量

# 修改QoS阈值倍数
# 编辑 evaluate.py 中的 QOS_MULTIPLIER（默认1.5）

# 修改敏感度曲线
# 编辑 worker.py 中的 sensitivity_threshold

# 修改补偿量
# 编辑 evaluate.py 中的 COMPENSATION_AMOUNT
```

## 技术栈

- **Python 3.9+**
- **Flask** - HTTP服务框架
- **requests** - HTTP客户端
- **pandas / numpy** - 数据分析
- **matplotlib / seaborn** - 可视化

## 学术论文参考

本项目灵感来自：
- Jiagu: 基于机器学习的在线函数超卖
- Microsoft Azure的在线函数调度研究
- AWS Lambda的性能隔离研究

## 许可证

MIT License

---

最后更新：2026-04-17
作者：ResilientCPU Team

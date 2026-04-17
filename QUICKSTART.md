# 实验执行指南

## ✅ 已部署完成

### Worker 机器 (172.31.26.175)
- ✅ worker.py 已部署并运行 (PID: 5640)
- ✅ 3个服务全部启动 (8081/8082/8083)
- ✅ Flask + requests 依赖已安装

### Controller 机器 (172.31.31.194)
- ✅ controller_scheduler.py 已创建
- ✅ evaluate.py 已创建（完整实验流程）
- ✅ Python依赖已安装 (pandas, numpy, matplotlib, seaborn, requests)
- ✅ 代码已推送到 GitHub

## 🚀 快速开始

### 第一步：验证Worker状态
```bash
# SSH到worker
ssh -i ~/.ssh/worker_rsa ec2-user@172.31.26.175

# 检查服务
curl http://localhost:8081/health
curl http://localhost:8082/health
curl http://localhost:8083/health

# 测试调用
curl -X POST http://localhost:8081/invoke | head -c 200
```

### 第二步：运行完整实验（推荐）

在 **Controller** 上执行：

```bash
cd /home/ec2-user/ResilientCPU

# 完整实验：三种方法 × 3次重复（需要约9小时）
python3 evaluate.py --methods baseline jiagu resilient --trials 3

# 或先快速测试（1分钟/方法）
python3 evaluate.py --methods baseline --duration 60 --trials 1
```

### 第三步：查看结果

```bash
# 实验结果目录
ls -lh /home/ec2-user/experiments/

# 查看汇总
cat /home/ec2-user/experiments/summary.csv

# 查看图表
ls -lh /home/ec2-user/experiments/*.png
```

## 📊 实验配置

### 当前实验参数
- 预热时间：2分钟
- 运行时长：1小时/次
- 重复次数：3次
- QPS变化：10→20→30→50→70→100→50→20→10
- QoS阈值：基准延迟 × 1.5

### 预期结果
| 方法 | CPU利用率 | QoS违反率 | 补偿响应时间 |
|------|-----------|-----------|--------------|
| Baseline | 20-30% | 0% | N/A |
| Jiagu-like | 50-60% | <5% | N/A |
| ResilientCPU | 65-75% | <5% | 毫秒级 ✅ |

## 🔧 单独测试每个组件

### 测试Worker服务
```bash
bash /home/ec2-user/ResilientCPU/test_quick.sh
```

### 测试调度器（5分钟）
```bash
cd /home/ec2-user/ResilientCPU
python3 controller_scheduler.py --method resilient --duration 300 --warmup 60
```

### 测试单次实验（30分钟）
```bash
python3 evaluate.py --method resilient --duration 1800 --trials 1
```

## 📁 文件清单

```
/home/ec2-user/ResilientCPU/
├── worker.py                   # Worker节点
├── controller_scheduler.py     # 调度器（3种策略）
├── evaluate.py                 # 实验评估
├── requirements.txt            # 依赖
├── README.md                  # 详细文档
├── DEPLOY.md                  # 部署指南
├── test_quick.sh              # 快速测试脚本
└── experiments/               # 实验结果（运行时生成）
    ├── summary.csv
    ├── comparison_plot.png
    └── baseline_trial1.json, ...
```

## 🐛 故障排查

### Worker无法连接
```bash
# 检查worker进程
ssh -i ~/.ssh/worker_rsa ec2-user@172.31.26.175 'ps aux | grep worker.py'

# 重启worker
ssh -i ~/.ssh/worker_rsa ec2-user@172.31.26.175 \
  'pkill -f worker.py && nohup python3 /home/ec2-user/worker.py > worker.log 2>&1 &'

# 查看日志
ssh -i ~/.ssh/worker_rsa ec2-user@172.31.26.175 'tail -50 /home/ec2-user/worker.log'
```

### Controller依赖问题
```bash
# 重新安装依赖
python3 -m pip install --user --force-reinstall pandas numpy matplotlib seaborn requests
```

### 实验中断恢复
```bash
# 查看当前进度
ls -lh /home/ec2-user/experiments/

# 继续运行（先删除已完成的方法）
rm /home/ec2-user/experiments/resilient_trial*.json
python3 evaluate.py --methods resilient --trials 3
```

## 📈 数据收集

实验会生成：
1. **JSON文件**：每次运行的详细数据
   - 延迟历史
   - 状态变化
   - 补偿事件
   - 补偿响应时间

2. **CSV汇总**：`summary.csv`
   - 每个方法的平均指标
   - 标准差

3. **对比图表**：
   - `comparison_plot.png` - 柱状图对比
   - `comparison_table.png` - 表格对比

## 🎯 验证ResilientCPU的优势

运行后重点观察：
1. **补偿响应时间**：ResilientCPU应该有毫秒级的恢复时间，而Baseline和Jiagu没有补偿
2. **QoS违反率**：ResilientCPU应在5%以下，且低于Jiagu
3. **CPU利用率**：ResilientCPU应达到65-75%，显著高于Baseline和Jiagu

## 📞 技术支持

- GitHub: https://github.com/luxm123/ResilientCPU
- 问题反馈：在GitHub提Issue

---

实验就绪，可以开始运行了！🎉


# ResilientCPU 实验部署指南

## 项目结构

```
ResilientCPU/
├── worker.py                    # Worker节点：3个HTTP服务 + 补偿机制
├── controller_scheduler.py      # Controller：调度器 + 负载生成
├── evaluate.py                  # 实验评估脚本
├── requirements.txt             # Python依赖
└── DEPLOY.md                   # 本文件
```

## 实验架构

### Worker 机器 (172.31.26.175)
- 运行 `worker.py`，启动3个HTTP服务：
  - **8081 (cpu_intensive)**: 计算圆周率5000万次
  - **8082 (io_mixed)**: 循环 sleep 10ms + 计算 5ms
  - **8083 (normal)**: 斐波那契数列计算
- 提供补偿接口：`POST /compensate` 借调CPU shares

### Controller 机器 (172.31.31.194)
- 运行 `controller_scheduler.py`：
  - 调度逻辑：基于敏感度曲线判断是否需要补偿
  - 负载生成器：Poisson过程，QPS 10-100
  - 记录请求延迟
- 运行 `evaluate.py`：
  - 对比三种方法（Baseline、Jiagu-like、ResilientCPU）
  - 每次运行1小时，重复3次
  - 收集CPU利用率、QoS违反率、调度密度

## 快速部署

### 1. 环境准备

**Worker 机器 (172.31.26.175)**：
```bash
# 检查Python版本
python3 --version  # 需要 3.9+

# 安装依赖
pip3 install --user flask requests

# 启动worker服务
python3 worker.py &
# 或使用nohup后台运行
nohup python3 worker.py > worker.log 2>&1 &
```

**Controller 机器 (172.31.31.194)**：
```bash
# 安装依赖
pip3 install --user flask requests pandas numpy matplotlib seaborn

# 运行调度器 + 负载生成
python3 controller_scheduler.py --qps 50 --duration 3600

# 或运行完整实验评估
python3 evaluate.py --duration 3600 --repeats 3
```

### 2. 验证部署

**Worker 健康检查**：
```bash
curl http://172.31.26.175:8081/health
curl http://172.31.26.175:8082/health
curl http://172.31.26.175:8083/health
```

**测试 invoke 接口**：
```bash
curl -X POST http://172.31.26.175:8081/invoke
curl -X POST http://172.31.26.175:8082/invoke
curl -X POST http://172.31.26.175:8083/invoke
```

**查看状态**：
```bash
curl http://172.31.26.175:8081/status
```

**测试补偿接口**：
```bash
curl -X POST http://172.31.26.175:8081/compensate \
  -H "Content-Type: application/json" \
  -d '{"donor": "normal", "amount": 256}'
```

### 3. 运行实验

**方式一：单独运行调度器**
```bash
# Controller 上运行
python3 controller_scheduler.py \
  --qps 50 \
  --duration 3600 \
  --log controller_log.jsonl
```

**方式二：完整对比实验**
```bash
# Controller 上运行
python3 evaluate.py \
  --duration 3600 \
  --repeats 3 \
  --methods baseline jiagu resilient
```

**实验输出**：
- 结果文件：`/home/ec2-user/experiments/`
  - `baseline_run1_results.json`, `jiagu_run1_results.json`, `resilient_run1_results.json`
  - `summary.csv` - 汇总数据
  - `comparison_plot.png` - 对比图表

## API 接口文档

### Worker 服务 (172.31.26.175)

#### 通用接口
- `GET /health` - 健康检查
- `GET /status` - 获取实时延迟统计
  ```json
  {
    "function": "cpu_intensive",
    "port": 8081,
    "sensitivity": 0.9,
    "cpu_shares": 1024,
    "avg_latency_ms": 21590.2,
    "max_latency_ms": 21590.2,
    "p99_latency_ms": 21590.2,
    "request_count": 1
  }
  ```
- `POST /compensate` - CPU shares 补偿
  ```json
  {
    "donor": "normal",    // donor函数 (normal/io_mixed)
    "amount": 256         // 借调数量
  }
  ```

#### 函数特定接口
- `POST http://172.31.26.175:8081/invoke` - cpu_intensive
- `POST http://172.31.26.175:8082/invoke` - io_mixed
- `POST http://172.31.26.175:8083/invoke` - normal

### Controller 服务 (172.31.31.194)

- `POST /schedule` - 手动触发调度（由 controller_scheduler.py 内部使用）

## 敏感度曲线

```python
SENSITIVITY_CURVE = {
    "cpu_intensive": {
        "threshold": 0.7,      # 拐点：CPU争抢>70%触发保护
        "baseline_shares": 1024,
        "compensation_factor": 1.5
    },
    "io_mixed": {
        "threshold": 0.5,
        "baseline_shares": 1024,
        "compensation_factor": 1.2
    },
    "normal": {
        "threshold": 0.3,
        "baseline_shares": 1024,
        "compensation_factor": 0.8  # 可作为donor
    }
}
```

## QoS 阈值

```python
QOS_THRESHOLDS = {
    "cpu_intensive": 100,   # ms
    "io_mixed": 200,        # ms
    "normal": 300           # ms
}
```

## 故障排查

### Worker 服务无法启动
```bash
# 查看日志
tail -f /home/ec2-user/worker.log

# 检查端口占用
netstat -tuln | grep 808

# 重启服务
pkill -f worker.py
nohup python3 worker.py > worker.log 2>&1 &
```

### Controller 无法连接 Worker
```bash
# 测试连接
curl http://172.31.26.175:8081/health

# 检查防火墙
sudo iptables -L

# 检查网络连通性
ping 172.31.26.175
```

### 实验数据收集
```bash
# 查看实验结果
ls -lh /home/ec2-user/experiments/

# 查看实时日志
tail -f /home/ec2-user/controller_log.jsonl
```

## 实验配置建议

| 参数 | 建议值 | 说明 |
|------|--------|------|
| QPS | 10-100 | Poisson过程变化 |
| 单次运行时长 | 3600秒 (1小时) | 足够收集稳定数据 |
| 重复次数 | 3次 | 取平均值减少方差 |
| CPU shares | 1024 (初始) | Docker默认值 |
| 补偿量 | 256 shares | 每次借调数量 |

## 预期结果

1. **Baseline**：无调度，QoS违反率高，CPU利用率不稳定
2. **Jiagu-like**：简单阈值补偿，效果有限，可能有震荡
3. **ResilientCPU**：基于敏感度曲线，QoS保障好，CPU利用率高

## 数据收集指标

- **CPU利用率**：基于CPU shares分配的加权平均
- **QoS违反率**：延迟超过阈值的请求比例
- **调度密度**：单位时间内的调度操作次数

## 扩展实验

可调整的参数：
- QPS 变化曲线（恒定/阶梯/随机）
- 函数敏感度阈值
- 补偿策略（固定/自适应）
- 负载模式（单一/混合/突发）

---

最后更新：2026-04-17
作者：ResilientCPU 实验系统

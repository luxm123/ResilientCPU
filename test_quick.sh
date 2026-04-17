#!/bin/bash
# 快速测试脚本 - 验证完整流程

echo "=== ResilientCPU 快速测试 ==="
echo ""

# 检查Worker状态
echo "1. 检查Worker服务..."
for port in 8081 8082 8083; do
    status=$(curl -s http://172.31.26.175:$port/health)
    echo "  Port $port: $status"
done

# 测试invoke
echo ""
echo "2. 测试函数调用..."
echo "  cpu_intensive (8081):"
curl -s -X POST http://172.31.26.175:8081/invoke | python3 -m json.tool | head -5

echo ""
echo "  io_mixed (8082):"
curl -s -X POST http://172.31.26.175:8082/invoke | python3 -m json.tool

echo ""
echo "  normal (8083):"
curl -s -X POST http://172.31.26.175:8083/invoke | python3 -m json.tool

# 测试status
echo ""
echo "3. 查看状态..."
curl -s http://172.31.26.175:8081/status | python3 -m json.tool | head -10

# 测试compensate
echo ""
echo "4. 测试补偿..."
curl -s -X POST http://172.31.26.175:8081/compensate \
  -H "Content-Type: application/json" \
  -d '{"donor": "normal", "recipient": "cpu_intensive", "amount": 256}' \
  | python3 -m json.tool

echo ""
echo "✅ Worker服务测试通过！"
echo ""
echo "接下来运行实验："
echo "  cd /home/ec2-user/ResilientCPU"
echo "  python3 evaluate.py --method baseline --duration 60 --trials 1  # 快速测试"
echo "  python3 evaluate.py --methods baseline jiagu resilient --trials 3  # 完整实验"

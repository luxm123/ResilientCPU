#!/bin/bash
# 部署脚本：将 controller 的 SSH 公钥添加到 worker 的 authorized_keys

CONTROLLER_IP="172.31.31.194"
WORKER_IP="172.31.26.175"

echo "=== 将 controller 的公钥部署到 worker ==="

# 从 controller 读取公钥
PUB_KEY=$(cat ~/.ssh/id_ed25519.pub)

# 通过 SSH 添加到 worker（需要在 controller 上执行）
ssh -i ~/.ssh/id_ed25519 ec2-user@$WORKER_IP "mkdir -p ~/.ssh && echo '$PUB_KEY' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"

echo "部署完成！现在可以 SSH 到 worker 了。"

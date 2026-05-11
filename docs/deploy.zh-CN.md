# Boji1334 Lite Faka 部署文档

[English](deploy.en-US.md)

## 适用场景

这个项目是轻量发卡网，目标是小服务器也能稳定运行。默认使用：

- Python 标准库 HTTP 服务
- SQLite 本地数据库
- systemd 进程守护
- 端口 `18080`
- 内存限制 `128M`

它不会安装 Docker、MySQL、PHP，也不会修改 nginx 配置。

## 服务器要求

- Ubuntu/Debian 系统
- Python 3.10+，脚本会在缺失时尝试安装
- git，脚本会在缺失时尝试安装
- 一个未占用端口，默认 `18080`

## 快速部署

```bash
curl -fsSL https://raw.githubusercontent.com/boji1334/boji1334-lite-faka/main/deploy/install.sh | sudo bash
```

脚本会做这些事情：

- 克隆仓库到 `/opt/boji1334-lite-faka`
- 创建运行用户 `litefaka`
- 创建数据目录 `/opt/boji1334-lite-faka/data`
- 创建环境文件 `/etc/boji1334-lite-faka.env`
- 创建 systemd 服务 `boji1334-lite-faka`
- 设置 `MemoryMax=128M` 和 `CPUQuota=30%`

## 推荐部署命令

如果你已经有域名，例如 `faka.example.com`，建议明确传入：

```bash
curl -fsSL https://raw.githubusercontent.com/boji1334/boji1334-lite-faka/main/deploy/install.sh -o /tmp/install-lite-faka.sh
sudo APP_PORT=18080 BASE_URL=http://faka.example.com:18080 SITE_NAME=轻量发卡网 bash /tmp/install-lite-faka.sh
```

如果要换端口：

```bash
sudo APP_PORT=18081 BASE_URL=http://faka.example.com:18081 bash /tmp/install-lite-faka.sh
```

## 阿里云安全组

直连端口访问时，需要在阿里云安全组开放 TCP 端口：

- 协议：TCP
- 端口：`18080`
- 授权对象：按你的需要填写，测试可以先用 `0.0.0.0/0`

如果后续走 nginx 反向代理 HTTPS，则外部只需要 `80/443`，应用可以改为监听 `127.0.0.1:18080`。

## 后台初始化

部署完成后，终端会输出后台地址、用户名和密码。

进入后台后建议立刻检查：

1. 商品管理：创建商品，填写价格。
2. 卡密管理：选择商品，一行一张导入卡密。
3. 支付设置：填写易支付网关、商户 ID、商户密钥。
4. 站点外部地址：直连端口时填写 `http://域名:18080`。

## 易支付配置

本项目使用常见易支付兼容参数：

- `pid`
- `type`，值为 `alipay` 或 `wxpay`
- `out_trade_no`
- `notify_url`
- `return_url`
- `name`
- `money`
- `sign`
- `sign_type=MD5`

后台填写网关时，可以填：

- `https://pay.example.com`
- 或者完整提交地址 `https://pay.example.com/submit.php`

支付回调成功后，应用会校验签名和金额，然后把订单标记为已支付并发出卡密。

## 常用命令

查看状态：

```bash
systemctl status boji1334-lite-faka --no-pager
```

查看日志：

```bash
journalctl -u boji1334-lite-faka -f
```

重启：

```bash
systemctl restart boji1334-lite-faka
```

查看端口：

```bash
ss -ltnp | grep ':18080'
```

查看内存：

```bash
systemctl status boji1334-lite-faka --no-pager
free -h
```

## 更新

重复执行安装脚本即可更新代码，数据目录不会被删除：

```bash
curl -fsSL https://raw.githubusercontent.com/boji1334/boji1334-lite-faka/main/deploy/install.sh | sudo bash
```

## 卸载

```bash
sudo systemctl disable --now boji1334-lite-faka
sudo rm -f /etc/systemd/system/boji1334-lite-faka.service
sudo systemctl daemon-reload
```

如果确认不再需要数据：

```bash
sudo rm -rf /opt/boji1334-lite-faka
sudo rm -f /etc/boji1334-lite-faka.env
sudo userdel litefaka 2>/dev/null || true
```

## 和现有服务的关系

安装脚本不会碰这些东西：

- nginx
- Docker / containerd
- sub2api
- sub2api-postgres
- sub2api-redis
- 现有网站路径，例如 `/sms`

如果你的服务器上已经有 `80/443` 服务，建议先用 `http://域名:18080` 测试，不要急着改 nginx。


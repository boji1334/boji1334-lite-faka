# Boji1334 Lite Faka

默认中文 | [English](docs/deploy.en-US.md)

一个极简发卡网：Python 标准库 + SQLite，适合 2 核 2G 这类小服务器。它不依赖 Docker、MySQL、Node，也不会改动你服务器上已有的 nginx、Docker、sub2api 或防火墙规则。

## 功能

- 商品上架、价格、描述、上下架
- 卡密一行一个批量导入
- 用户下单、订单查询、支付后自动显示卡密
- 易支付兼容接口，支持 `alipay` 和 `wxpay`
- 后台管理商品、库存、订单、支付配置
- systemd 部署，默认 `MemoryMax=128M`、`CPUQuota=30%`

## 一条命令部署

在服务器上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/boji1334/boji1334-lite-faka/main/deploy/install.sh | sudo bash
```

默认监听 `0.0.0.0:18080`。如果你使用阿里云安全组直连端口，需要开放 TCP `18080`。

指定端口和域名：

```bash
curl -fsSL https://raw.githubusercontent.com/boji1334/boji1334-lite-faka/main/deploy/install.sh -o /tmp/install-lite-faka.sh
sudo APP_PORT=18080 BASE_URL=http://faka.example.com:18080 bash /tmp/install-lite-faka.sh
```

部署完成后访问：

- 前台：`http://你的域名:18080`
- 后台：`http://你的域名:18080/admin`

完整中文文档见：[docs/deploy.zh-CN.md](docs/deploy.zh-CN.md)

## 本地运行

```bash
python3 app.py
```

打开 `http://127.0.0.1:18080`。

默认后台：

- 用户名：`admin`
- 密码：`change-me-now`

生产环境必须通过环境变量或部署脚本修改默认密码。

## 易支付参数

后台进入「支付设置」，填写：

- 易支付网关：例如 `https://pay.example.com`
- 商户 ID：易支付平台给你的 `pid`
- 商户密钥：易支付平台给你的 `key`
- 站点外部地址：例如 `http://faka.example.com:18080`

回调地址会自动生成：`站点外部地址/notify`。

## 安全边界

部署脚本只安装并启动 `boji1334-lite-faka` 这个 systemd 服务，不会操作：

- Docker / containerd
- sub2api、sub2api-postgres、sub2api-redis
- nginx
- 服务器防火墙或云安全组
- 现有网站路径，例如 `/sms`


# Boji1334 Lite Faka Deployment Guide

[中文](deploy.zh-CN.md)

## What It Is

Boji1334 Lite Faka is a small digital-card storefront for low-memory servers. It uses:

- Python standard library HTTP server
- SQLite
- systemd
- Default port `18080`
- Default memory cap `128M`

It does not install Docker, MySQL, PHP, or modify nginx.

## Requirements

- Ubuntu/Debian server
- Python 3.10+
- git
- An unused port, default `18080`

The installer tries to install Python and git with `apt-get` if they are missing.

## One-Command Install

```bash
curl -fsSL https://raw.githubusercontent.com/boji1334/boji1334-lite-faka/main/deploy/install.sh | sudo bash
```

The installer will:

- Clone the repo to `/opt/boji1334-lite-faka`
- Create a system user `litefaka`
- Create `/opt/boji1334-lite-faka/data`
- Create `/etc/boji1334-lite-faka.env`
- Create and start the systemd service
- Apply `MemoryMax=128M` and `CPUQuota=30%`

## Recommended Install

With a domain:

```bash
curl -fsSL https://raw.githubusercontent.com/boji1334/boji1334-lite-faka/main/deploy/install.sh -o /tmp/install-lite-faka.sh
sudo APP_PORT=18080 BASE_URL=http://faka.example.com:18080 SITE_NAME="Lite Faka" bash /tmp/install-lite-faka.sh
```

Use another port:

```bash
sudo APP_PORT=18081 BASE_URL=http://faka.example.com:18081 bash /tmp/install-lite-faka.sh
```

## Firewall Or Cloud Security Group

For direct port access, open TCP `18080` in your cloud security group.

If you later put it behind nginx and HTTPS, expose only `80/443` publicly and proxy to `127.0.0.1:18080`.

## Admin Setup

The installer prints the admin URL, username, and password.

In the admin panel:

1. Create a product.
2. Import card codes, one per line.
3. Configure the EPay-compatible gateway, merchant ID, and merchant key.
4. Set the public base URL, such as `http://faka.example.com:18080`.

## EPay-Compatible Payment

The app submits common EPay-compatible fields:

- `pid`
- `type`, either `alipay` or `wxpay`
- `out_trade_no`
- `notify_url`
- `return_url`
- `name`
- `money`
- `sign`
- `sign_type=MD5`

The notify endpoint validates the signature and amount before marking the order as paid and delivering a card code.

## Useful Commands

Status:

```bash
systemctl status boji1334-lite-faka --no-pager
```

Logs:

```bash
journalctl -u boji1334-lite-faka -f
```

Restart:

```bash
systemctl restart boji1334-lite-faka
```

Port:

```bash
ss -ltnp | grep ':18080'
```

## Update

Run the installer again. The data directory is preserved.

```bash
curl -fsSL https://raw.githubusercontent.com/boji1334/boji1334-lite-faka/main/deploy/install.sh | sudo bash
```

## Uninstall

```bash
sudo systemctl disable --now boji1334-lite-faka
sudo rm -f /etc/systemd/system/boji1334-lite-faka.service
sudo systemctl daemon-reload
```

Remove data only if you no longer need it:

```bash
sudo rm -rf /opt/boji1334-lite-faka
sudo rm -f /etc/boji1334-lite-faka.env
sudo userdel litefaka 2>/dev/null || true
```

## Existing Services

The installer does not touch:

- nginx
- Docker / containerd
- sub2api
- sub2api-postgres
- sub2api-redis
- Existing website paths such as `/sms`


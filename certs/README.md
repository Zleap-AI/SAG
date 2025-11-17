# SSL 证书目录

此目录用于存放 SSL/TLS 证书文件，用于 HTTPS 加密通信。

## 📋 快速使用

### 方式 1：放置现有证书（推荐）

如果你已有证书，将以下文件复制到此目录：

```bash
certs/
├── fullchain.pem    # 完整证书链（必需）
└── privkey.pem      # 私钥文件（必需）
```

然后运行部署脚本：
```bash
./scripts/deploy.sh
```

部署脚本会自动检测证书并启用 HTTPS。

### 方式 2：Let's Encrypt 自动证书

使用 Certbot 自动获取免费证书：

```bash
# 安装 Certbot
sudo apt-get install certbot

# 获取证书（需要域名指向服务器）
sudo certbot certonly --standalone -d yourdomain.com

# 复制证书到项目
sudo cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem certs/
sudo cp /etc/letsencrypt/live/yourdomain.com/privkey.pem certs/
sudo chmod 644 certs/*.pem
```

### 方式 3：使用其他 CA 证书

如果使用其他证书颁发机构（如阿里云、腾讯云），确保文件命名为：
- `fullchain.pem` - 包含服务器证书和中间证书的完整链
- `privkey.pem` - 服务器私钥

## 🔒 证书格式要求

### fullchain.pem
应包含完整证书链（PEM 格式）：
```
-----BEGIN CERTIFICATE-----
[服务器证书内容]
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
[中间证书内容]
-----END CERTIFICATE-----
```

### privkey.pem
私钥文件（PEM 格式，RSA 或 ECDSA）：
```
-----BEGIN PRIVATE KEY-----
[私钥内容]
-----END PRIVATE KEY-----
```

## 🔄 证书更新

### 手动更新
```bash
# 1. 备份旧证书
cp certs/fullchain.pem certs/fullchain.pem.bak
cp certs/privkey.pem certs/privkey.pem.bak

# 2. 替换新证书
cp /path/to/new/fullchain.pem certs/
cp /path/to/new/privkey.pem certs/

# 3. 重启 Nginx
docker compose restart nginx
```

### Let's Encrypt 自动续期
```bash
# 设置自动续期（每天检查）
sudo crontab -e

# 添加以下行
0 3 * * * certbot renew --post-hook "cp /etc/letsencrypt/live/yourdomain.com/*.pem /path/to/project/certs/ && docker compose -f /path/to/project/docker-compose.yml restart nginx"
```

## ⚠️ 安全提示

1. **权限设置**：证书文件应设置为只读
   ```bash
   chmod 644 certs/*.pem
   ```

2. **不要提交证书**：`.gitignore` 已配置，确保证书不会被提交到 Git

3. **备份证书**：定期备份证书和私钥到安全位置

4. **监控过期**：证书通常 90 天过期，建议提前 30 天续期

## 🆘 故障排查

### 问题：Nginx 无法启动
```bash
# 检查证书文件是否存在
ls -lh certs/

# 检查证书有效性
openssl x509 -in certs/fullchain.pem -text -noout

# 检查私钥匹配
openssl x509 -noout -modulus -in certs/fullchain.pem | openssl md5
openssl rsa -noout -modulus -in certs/privkey.pem | openssl md5
# 两个输出应该一致
```

### 问题：证书不匹配域名
```bash
# 查看证书支持的域名
openssl x509 -in certs/fullchain.pem -text -noout | grep -A1 "Subject Alternative Name"
```

## 📚 更多信息

详细的 SSL 配置指南请参考：
- [完整部署文档](../docs/deploy/README.md)
- [SSL 配置指南](../docs/deploy/03-ssl-setup.md)

# @zleap-ai/dsh-sag

dsh-sag 让 DeepSeek Harness 使用 SAG 个人知识库，包括检索、阅读、上传、文本写入和文档管理。

## 兼容版本

- 本源码插件：`0.2.0`；DeepSeek Harness：`0.2.0-rc.2`。
- Node.js：`^22.19.0` 或 `>=24.0.0`，推荐 Node.js 24；`dsh` 和 `pnpm` 需要在 PATH 中。
- SAG：包含“连接 dsh”设置和本地连接器 API。
- embedded 引擎：`zleap-sag==0.14.0`，详见[高级配置](docs/embedded.md)。

旧插件 `0.1.1` 用于旧版 dsh，不能安装到 dsh 0.2。其他宿主版本未验证。

## 开始使用

先启动 SAG。首次安装 dsh 可运行 `npm install --global @deepseek-ai/dsh@0.2.0-rc.2`。

### 从 npm 安装

用户通过 npm 安装插件。以下命令在 `0.2.0` 发布到 npm 后可用。

```sh
dsh plugin --profile web add @zleap-ai/dsh-sag@0.2.0
dsh plugin --profile web exec dsh-sag doctor
dsh --profile web
```

`doctor` 显示“SAG 已连接”后，启动或重启 Web 即可使用插件。本机通常自动连接，无需 Python 或手工填写密钥。

## 连接 SAG

如果自动发现没有成功，任选一种方式保存连接：

```sh
# 自动发现已经启动的 SAG
dsh plugin --profile web exec dsh-sag setup

# 使用 SAG“连接 dsh”设置导出的连接文件
dsh plugin --profile web exec dsh-sag setup ./sag-dsh.json

# 指定 SAG 的本机地址
dsh plugin --profile web exec dsh-sag setup --url http://127.0.0.1:8000
```

使用导出文件时，请在文件所在目录执行命令，或将 `./sag-dsh.json` 替换为实际路径。配置后运行 `dsh plugin --profile web exec dsh-sag doctor`，再重启 dsh。所有命令使用同一 profile 和相同的 `DSH_HOME`；自定义 profile 请一致替换 `web`。

## 直接告诉 dsh

- “在 SAG 知识库里查找上传限制，并给出原文依据。”
- “把 `/Users/me/Documents/产品手册.pdf` 上传到 SAG，处理完成后总结内容。”
- “把下面的会议结论作为笔记写入 SAG：……”

SAG 会声明当前可用的操作；缺少的能力不会被调用。删除文档需要用户确认。

## 开发与源码安装

开发或调试插件修改时，在 SAG 源码仓库根目录构建本地包。

```sh
cd integrations/dsh-sag
pnpm install --frozen-lockfile
pnpm run pack
dsh plugin --profile web add ./artifacts/zleap-ai-dsh-sag-0.2.0.tgz
```

随后运行 `dsh plugin --profile web exec dsh-sag doctor`，再启动或重启对应 profile。

## 升级与恢复

升级宿主后，显式安装匹配的新插件。之前精确固定为旧版本的安装可能不会被不带版本的 `update` 替换；安装 `0.2.0` 后重新执行 `setup` 和 `doctor`，连接凭据仍由 dsh Credentials 保存。

若插件阻止 Web 启动，可以先运行：

```sh
dsh plugin --profile web remove @zleap-ai/dsh-sag
dsh --profile web
```

卸载插件不会删除 SAG 文档。无需删除整个 dsh 配置目录；如果曾手工添加 Cordis 配置，只移除对应插件条目。

源码与独立发布位于 [SAG 仓库](https://github.com/Zleap-AI/SAG/tree/main/integrations/dsh-sag)。需要直接托管 Python 引擎时，参阅[embedded 模式](docs/embedded.md)。

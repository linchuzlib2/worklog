# 工作簿

Flask 日常工作记录应用，支持任务、富文本笔记、任务关联和附件上传。

## 本地运行

```powershell
python -m pip install -r requirements.txt
python app.py
```

打开 http://127.0.0.1:5000。

## 阿里云 OSS

应用使用阿里云 OSS 的 S3 兼容 API。复制 `.env.example` 为 `.env`，填写：

- `OSS_ENDPOINT`
- `OSS_BUCKET`
- `OSS_REGION`
- `OSS_ACCESS_KEY_ID`
- `OSS_SECRET_ACCESS_KEY`

附件直接写入 OSS 的 `attachments/` 前缀；SQLite 数据库在启动时从 `OSS_DATABASE_KEY` 恢复，每次任务、笔记或附件元数据变更后同步回 OSS。生产环境建议创建仅允许访问指定 Bucket 的 RAM 子账号，不要使用主账号密钥。

## Render 部署

项目包含 `render.yaml`。将代码推送到 GitHub 后，在 Render 选择 **New Blueprint Instance**，连接仓库并按提示填写 OSS 环境变量。Render 的 Web Service 使用 `gunicorn app:app` 启动。

Render 不保存本地数据库和附件：数据库由 OSS 同步，附件上传后直接进入 OSS。首次部署前请先创建 OSS Bucket，并为 RAM 子账号配置该 Bucket 的读写权限。

## EPUB 阅读与文档编辑

文件管理中的 `.epub` 可在线按目录阅读、翻章、调节字号并记忆阅读进度；EPUB 章节内容会经过 HTML 清洗。EPUB 也可用本机默认电子书编辑程序修改。`.doc`、`.docx`、`.xls`、`.xlsx` 和 `.pdf` 使用 Windows 本机默认程序编辑。

1. 在 Render 环境变量中设置 `LOCAL_EDITOR_TOKEN`，使用至少 32 个字符的随机密钥。
2. 每台要编辑文件的 Windows 电脑首次安装或助手更新后，都在文件管理页同时下载 `Install-LocalEditor.bat` 和 `Install-LocalEditor.ps1` 到同一文件夹，再双击 `.bat`。按提示输入网站根地址和相同密钥。密钥会用当前 Windows 用户的 DPAPI 加密保存；文件本身从云端获取，因此不需要在电脑间拷贝文档。
3. EPUB 可点“在线阅读”；点“本机编辑”会由系统默认程序打开支持的文档，保存后自动推送回 OSS。后台 PowerShell 窗口会监视最多 24 小时并自动上传；关闭窗口会停止监视。助手更新后需在每台电脑重新下载并运行安装程序。

助手通过 SHA-256 版本校验避免用旧副本覆盖云端新版本。遇到版本冲突时关闭本地文件并从网页重新打开。网站和本机都不要公开或分享同步密钥。

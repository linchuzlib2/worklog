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

# Codex 辅助安装

把下面提示词交给目标电脑上的 Codex：

```text
请把 https://github.com/Su-Chen-Love/daily-literature 安装成私有的本地单用户应用。

自主执行可恢复步骤：检查 Python 3.9+；安全克隆或更新代码且不覆盖本地改动；创建 .venv 并安装；运行 `academic-radar setup`；验证数据库完整性、已激活画像、/healthz 和六个页面；最后报告 http://127.0.0.1:8765、私有状态目录与日志路径。

若发现旧 state，先预览并备份，再迁移和验证；不得删库重建。若端口占用，先确认是否为健康的 Academic Radar，不要终止无关进程。macOS 使用 setup 安装可逆的用户级服务；Linux/Windows 只完成初始化与验证，并明确说明后台服务仍需人工配置。

不要配置任何独立模型 API，不要读取或打印环境变量中的密钥，不要启用远程绑定、云同步、共享数据库或公共网站。配置、SQLite、画像、反馈、队列、结果、PDF、日志和备份必须留在私有状态目录，不能提交 GitHub。
```

成功交付应包含：所用 Python/虚拟环境、升级前备份、schema 与 integrity 结果、六页 HTTP 结果、服务模式、本地 URL、状态/日志位置，以及仍需首次采集或 Codex 判断的诚实提示。

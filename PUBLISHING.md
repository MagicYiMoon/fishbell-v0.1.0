# 发布鱼铃

建议用 `fishbell` 作为 GitHub 仓库名。解压源码包后，把包含 `README.md`、`metadata.yaml`、`main.py`、`harness/` 和 `.github/` 的目录作为仓库根目录。AstrBot 从根目录读取插件，Harness 插件保持在 `harness/`。两版内部插件标识保持原样，方便保留现有配置。

新仓库建好后，建议在根目录的 `metadata.yaml` 补上真实的仓库地址，让 AstrBot 能识别来源及检查更新。下面是格式示例，请替换成实际地址：

```yaml
repo: https://github.com/YOUR_ACCOUNT/fishbell
```

## GitHub Release

- 标题：**鱼铃 v0.1.0｜AstrBot / DeepSeek Harness**
- 标签：`fishbell-v0.1.0`
- 附件：`fishbell-astrbot-0.1.0.zip`、`fishbell-harness-0.1.0.zip` 及各自的 `.zip.sha256`。

如果重新制作附件，AstrBot ZIP 顶层目录使用 `astrbot_plugin_fishworld/`，其中直接包含根目录的 AstrBot 文件；无需放入 `harness/`。Harness ZIP 只包含 `harness/` 下的内容，将它们放入 `fishworld-harness/`，让 `package.json` 和 `cordis.patch.yml` 直接位于该目录。原先的 AstrBot ZIP 如果仍指向旧仓库，先更新其中的元数据并重新生成 SHA-256，再上传到新仓库。GitHub 生成的 `Source code.zip` 是整仓源码，用户应按宿主选择对应的安装包。

## 发布前检查

```bash
python -m pip install -r requirements.txt ruff
ruff check .
ruff format --check .
python -m unittest discover -s tests -q
cd harness
node --test
```

源码采用 MIT 许可证。提交时保留授权文件，排除真实配置、观看密钥、运行缓存和日志。不要把测试通过当作真实账号的 MCP 联调结果。
